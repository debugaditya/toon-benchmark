#include <pybind11/pybind11.h>
#include <pybind11/stl.h>
#include <string>
#include <vector>
#include <charconv>
#include <cctype>
#include <stdexcept>

namespace py = pybind11;

static bool looks_like_number(const char* s, size_t len) {
    if (len == 0) return false;
    size_t i = 0;
    if (s[i] == '-' || s[i] == '+') i++;
    if (i >= len) return false;
    bool seen_digit = false, seen_dot = false;
    for (; i < len; i++) {
        if (isdigit((unsigned char)s[i])) { seen_digit = true; continue; }
        if (s[i] == '.' && !seen_dot) { seen_dot = true; continue; }
        return false;
    }
    return seen_digit;
}

static bool looks_like_number(const std::string& s) { return looks_like_number(s.data(), s.size()); }

static void append_string_field(std::string& out, const std::string& s) {
    bool needs_quote = s.empty() || s.front() == ' ' || s.back() == ' ' ||
                       s == "true" || s == "false" || s == "null" || looks_like_number(s);
    if (!needs_quote) {
        for (char c : s) {
            if (c == ',' || c == '"' || c == '\\' || c == '\n') { needs_quote = true; break; }
        }
    }
    if (!needs_quote) {
        out += s;
        return;
    }
    out += '"';
    for (char c : s) {
        if (c == '\\') out += "\\\\";
        else if (c == '"') out += "\\\"";
        else if (c == '\n') out += "\\n";
        else out += c;
    }
    out += '"';
}

static void append_scalar(std::string& out, PyObject* v) {
    if (v == Py_None) { out += "null"; return; }
    if (PyBool_Check(v)) { out += (v == Py_True) ? "true" : "false"; return; }
    if (PyLong_Check(v)) {
        long long val = PyLong_AsLongLong(v);
        char buf[24];
        auto res = std::to_chars(buf, buf + sizeof(buf), val);
        out.append(buf, res.ptr - buf);
        return;
    }
    if (PyFloat_Check(v)) {
        double val = PyFloat_AsDouble(v);
        char buf[32];
        auto res = std::to_chars(buf, buf + sizeof(buf), val);
        out.append(buf, res.ptr - buf);
        return;
    }
    if (PyUnicode_Check(v)) {
        Py_ssize_t len;
        const char* data = PyUnicode_AsUTF8AndSize(v, &len);
        if (!data) throw std::runtime_error("append_scalar: failed to decode string as UTF-8");
        append_string_field(out, std::string(data, len));
        return;
    }
    throw std::runtime_error("append_scalar: unsupported scalar type (only str/int/float/bool/None supported)");
}

std::string encode_flat(const py::list& rows) {
    if (rows.size() == 0) return "[0]{}:";

    py::dict first = rows[0].cast<py::dict>();
    std::vector<std::string> fields;
    std::vector<py::str> field_keys;
    fields.reserve(py::len(first));
    field_keys.reserve(py::len(first));
    for (auto item : first) {
        std::string k = py::str(item.first).cast<std::string>();
        field_keys.push_back(py::str(k));
        fields.push_back(std::move(k));
    }

    std::string out;
    out.reserve(rows.size() * (fields.size() * 8 + 8));

    out += '[';
    { char buf[24]; auto r = std::to_chars(buf, buf + sizeof(buf), (long long)rows.size()); out.append(buf, r.ptr - buf); }
    out += "]{";
    for (size_t i = 0; i < fields.size(); i++) {
        if (i) out += ',';
        out += fields[i];
    }
    out += "}:";

    for (auto row_h : rows) {
        PyObject* row = row_h.ptr();
        out += "\n  ";
        for (size_t i = 0; i < fields.size(); i++) {
            if (i) out += ',';
            PyObject* val = PyDict_GetItem(row, field_keys[i].ptr());
            if (!val) {
                throw std::runtime_error("encode_flat: row missing field '" + fields[i] + "' -- all rows must share the same keys");
            }
            append_scalar(out, val);
        }
    }
    return out;
}

static py::object type_field(const std::string& raw, bool was_quoted) {
    if (was_quoted) return py::str(raw);
    if (raw == "true") return py::bool_(true);
    if (raw == "false") return py::bool_(false);
    if (raw == "null") return py::none();
    if (looks_like_number(raw)) {
        if (raw.find('.') != std::string::npos) return py::float_(std::stod(raw));
        try { return py::int_(std::stoll(raw)); }
        catch (...) { return py::float_(std::stod(raw)); }
    }
    return py::str(raw);
}

static std::vector<std::pair<std::string, bool>> split_row_typed(const std::string& line) {
    std::vector<std::pair<std::string, bool>> fields;
    size_t i = 0, n = line.size();
    while (i <= n) {
        std::string field;
        bool quoted = false;
        if (i < n && line[i] == '"') {
            quoted = true;
            i++;
            while (i < n) {
                char c = line[i];
                if (c == '\\' && i + 1 < n) {
                    char nc = line[i + 1];
                    if (nc == '\\') { field += '\\'; i += 2; continue; }
                    if (nc == '"') { field += '"'; i += 2; continue; }
                    if (nc == 'n') { field += '\n'; i += 2; continue; }
                    field += c; i++; continue;
                }
                if (c == '"') { i++; break; }
                field += c; i++;
            }
        } else {
            while (i < n && line[i] != ',') { field += line[i]; i++; }
        }
        fields.push_back({field, quoted});
        if (i < n && line[i] == ',') { i++; continue; }
        break;
    }
    return fields;
}

py::list decode_flat(const std::string& text) {
    std::istringstream stream(text);
    std::string header;
    std::getline(stream, header);

    size_t lb = header.find('['), rb = header.find(']'), cb = header.find('{'), cbe = header.find('}');
    if (lb == std::string::npos || rb == std::string::npos || cb == std::string::npos || cbe == std::string::npos) {
        throw std::runtime_error("decode_flat: malformed header, expected '[N]{fields}:'");
    }
    long n = std::stol(header.substr(lb + 1, rb - lb - 1));
    std::string field_str = header.substr(cb + 1, cbe - cb - 1);

    std::vector<std::string> fields;
    if (!field_str.empty()) {
        std::istringstream fs(field_str);
        std::string f;
        while (std::getline(fs, f, ',')) fields.push_back(f);
    }

    py::list rows;
    std::string line;
    for (long r = 0; r < n; r++) {
        if (!std::getline(stream, line)) {
            throw std::runtime_error("decode_flat: expected " + std::to_string(n) + " rows, stream ended early");
        }
        size_t start = 0;
        while (start < line.size() && line[start] == ' ') start++;
        std::string content = line.substr(start);

        auto typed_fields = split_row_typed(content);
        if (typed_fields.size() != fields.size()) {
            throw std::runtime_error("decode_flat: row " + std::to_string(r) + " has " +
                                      std::to_string(typed_fields.size()) + " fields, expected " +
                                      std::to_string(fields.size()));
        }
        py::dict row;
        for (size_t i = 0; i < fields.size(); i++) {
            row[py::str(fields[i])] = type_field(typed_fields[i].first, typed_fields[i].second);
        }
        rows.append(row);
    }
    return rows;
}

struct SchemaNode {
    enum Kind { SCALAR, OBJECT, ARRAY };
    Kind kind = SCALAR;
    std::string name;
    std::vector<SchemaNode> children;
};

static void merge_schema(SchemaNode& node, PyObject* value) {
    if (value == Py_None) return;

    if (PyDict_Check(value)) {
        if (node.kind != SchemaNode::OBJECT) {
            node.kind = SchemaNode::OBJECT;
            PyObject *key, *child_value;
            Py_ssize_t pos = 0;
            while (PyDict_Next(value, &pos, &key, &child_value)) {
                if (!PyUnicode_Check(key)) {
                    throw std::runtime_error("encode_nested: object keys must be strings");
                }
                Py_ssize_t klen;
                const char* kdata = PyUnicode_AsUTF8AndSize(key, &klen);
                SchemaNode child;
                child.name = std::string(kdata, klen);
                merge_schema(child, child_value);
                node.children.push_back(child);
            }
        } else {
            for (size_t i = 0; i < node.children.size(); ++i) {
                PyObject* child_value = PyDict_GetItemString(value, node.children[i].name.c_str());
                if (child_value) {
                    merge_schema(node.children[i], child_value);
                }
            }
        }
    } else if (PyList_Check(value) || PyTuple_Check(value)) {
        if (node.kind != SchemaNode::ARRAY) {
            node.kind = SchemaNode::ARRAY;
        }
        if (node.children.empty()) {
            Py_ssize_t len = PyList_Check(value) ? PyList_Size(value) : PyTuple_Size(value);
            if (len > 0) {
                PyObject* first = PyList_Check(value) ? PyList_GetItem(value, 0) : PyTuple_GetItem(value, 0);
                SchemaNode child;
                merge_schema(child, first);
                node.children.push_back(child);
            }
        }
    }
}

static SchemaNode build_row_schema(const py::list& rows) {
    SchemaNode root;
    root.kind = SchemaNode::OBJECT;

    if (rows.size() == 0) return root;

    PyObject* first_row = rows[0].ptr();
    if (!PyDict_Check(first_row)) {
        throw std::runtime_error("encode_nested: every row must be a dictionary");
    }

    PyObject *key, *value;
    Py_ssize_t pos = 0;
    while (PyDict_Next(first_row, &pos, &key, &value)) {
        if (!PyUnicode_Check(key)) {
            throw std::runtime_error("encode_nested: row keys must be strings");
        }
        Py_ssize_t klen;
        const char* kdata = PyUnicode_AsUTF8AndSize(key, &klen);
        SchemaNode child;
        child.name = std::string(kdata, klen);
        root.children.push_back(child);
    }

    for (auto row_h : rows) {
        PyObject* row = row_h.ptr();
        if (!PyDict_Check(row)) continue;
        for (size_t i = 0; i < root.children.size(); ++i) {
            PyObject* child_value = PyDict_GetItemString(row, root.children[i].name.c_str());
            if (child_value) {
                merge_schema(root.children[i], child_value);
            }
        }
    }

    return root;
}

static void append_schema_node(std::string& out, const SchemaNode& node) {
    out += node.name;
    if (node.kind == SchemaNode::OBJECT) {
        out += '{';
        for (size_t i = 0; i < node.children.size(); ++i) {
            if (i) out += ',';
            append_schema_node(out, node.children[i]);
        }
        out += '}';
    }
}

static void append_schema(std::string& out, const SchemaNode& root) {
    for (size_t i = 0; i < root.children.size(); ++i) {
        if (i) out += ',';
        append_schema_node(out, root.children[i]);
    }
}

static PyObject* dict_get_required(PyObject* dict, const std::string& key) {
    PyObject* value = PyDict_GetItemString(dict, key.c_str());
    if (!value) {
        throw std::runtime_error("encode_nested: row missing field '" + key + "'");
    }
    return value;
}

static void append_nested_value(std::string& out, const SchemaNode& node, PyObject* value) {
    if (node.kind == SchemaNode::SCALAR) {
        append_scalar(out, value);
        return;
    }

    if (node.kind == SchemaNode::OBJECT) {
        if (value == Py_None) {
            out += "null";
            return;
        }
        if (!PyDict_Check(value)) {
            throw std::runtime_error("encode_nested: expected object for field '" + node.name + "'");
        }
        out += '{';
        for (size_t i = 0; i < node.children.size(); ++i) {
            if (i) out += ',';
            const SchemaNode& child = node.children[i];
            PyObject* child_value = dict_get_required(value, child.name);
            append_nested_value(out, child, child_value);
        }
        out += '}';
        return;
    }

    if (value == Py_None) {
        out += "null";
        return;
    }
    Py_ssize_t len = 0;
    if (PyList_Check(value)) {
        len = PyList_Size(value);
    } else if (PyTuple_Check(value)) {
        len = PyTuple_Size(value);
    } else {
        throw std::runtime_error("encode_nested: expected array for field '" + node.name + "'");
    }

    out += '[';
    {
        char buf[24];
        auto r = std::to_chars(buf, buf + sizeof(buf), (long long)len);
        out.append(buf, r.ptr - buf);
    }
    out += "]{";
    for (Py_ssize_t i = 0; i < len; ++i) {
        if (i) out += ',';
        PyObject* item = PyList_Check(value) ? PyList_GetItem(value, i) : PyTuple_GetItem(value, i);
        append_scalar(out, item);
    }
    out += '}';
}

static void validate_schema_compatible(const SchemaNode& schema, PyObject* value) {
    if (schema.kind == SchemaNode::SCALAR) {
        return;
    }

    if (schema.kind == SchemaNode::OBJECT) {
        if (!PyDict_Check(value)) {
            throw std::runtime_error("encode_nested: schema mismatch for object '" + schema.name + "'");
        }
        for (const SchemaNode& child : schema.children) {
            PyObject* child_value = dict_get_required(value, child.name);
            if (child_value != Py_None) {
                validate_schema_compatible(child, child_value);
            }
        }
        return;
    }

    if (value == Py_None) return;

    if (!PyList_Check(value) && !PyTuple_Check(value)) {
        throw std::runtime_error("encode_nested: schema mismatch for array '" + schema.name + "'");
    }
    if (schema.children.empty()) return;
    Py_ssize_t len = PyList_Check(value) ? PyList_Size(value) : PyTuple_Size(value);
    for (Py_ssize_t i = 0; i < len; ++i) {
        PyObject* item = PyList_Check(value) ? PyList_GetItem(value, i) : PyTuple_GetItem(value, i);
        validate_schema_compatible(schema.children[0], item);
    }
}

static std::string encode_nested(const py::list& rows) {
    if (rows.size() == 0) {
        return "[0]{}:";
    }

    SchemaNode schema = build_row_schema(rows);

    for (auto row_h : rows) {
        PyObject* row = row_h.ptr();
        if (!PyDict_Check(row)) {
            throw std::runtime_error("encode_nested: every row must be a dictionary");
        }
        for (const SchemaNode& field : schema.children) {
            PyObject* value = dict_get_required(row, field.name);
            if (value != Py_None) {
                validate_schema_compatible(field, value);
            }
        }
    }

    std::string out;
    out.reserve(rows.size() * 48 + 128);

    out += '[';
    {
        char buf[24];
        auto r = std::to_chars(buf, buf + sizeof(buf), (long long)rows.size());
        out.append(buf, r.ptr - buf);
    }
    out += "]{";
    append_schema(out, schema);
    out += "}:";

    for (auto row_h : rows) {
        PyObject* row = row_h.ptr();
        out += "\n  ";
        for (size_t i = 0; i < schema.children.size(); ++i) {
            if (i) out += ',';
            const SchemaNode& field = schema.children[i];
            PyObject* value = dict_get_required(row, field.name);
            append_nested_value(out, field, value);
        }
    }

    return out;
}

static size_t find_matching_brace(const std::string& s, size_t open) {
    int depth = 0;
    bool quoted = false;
    bool escaped = false;

    for (size_t i = open; i < s.size(); ++i) {
        char c = s[i];
        if (quoted) {
            if (escaped) {
                escaped = false;
            } else if (c == '\\') {
                escaped = true;
            } else if (c == '"') {
                quoted = false;
            }
            continue;
        }
        if (c == '"') {
            quoted = true;
            continue;
        }
        if (c == '{') ++depth;
        else if (c == '}') {
            --depth;
            if (depth == 0) return i;
        }
    }
    throw std::runtime_error("decode_nested: unmatched '{'");
}

static std::vector<std::string> split_top_level(const std::string& s, char delimiter) {
    std::vector<std::string> parts;
    size_t start = 0;
    int brace_depth = 0;
    int bracket_depth = 0;
    bool quoted = false;
    bool escaped = false;

    for (size_t i = 0; i < s.size(); ++i) {
        char c = s[i];
        if (quoted) {
            if (escaped) escaped = false;
            else if (c == '\\') escaped = true;
            else if (c == '"') quoted = false;
            continue;
        }
        if (c == '"') {
            quoted = true;
        } else if (c == '{') {
            ++brace_depth;
        } else if (c == '}') {
            --brace_depth;
        } else if (c == '[') {
            ++bracket_depth;
        } else if (c == ']') {
            --bracket_depth;
        } else if (c == delimiter && brace_depth == 0 && bracket_depth == 0) {
            parts.push_back(s.substr(start, i - start));
            start = i + 1;
        }
    }
    parts.push_back(s.substr(start));
    return parts;
}

struct NestedSchema {
    std::string name;
    enum Kind { SCALAR, OBJECT, ARRAY } kind = SCALAR;
    std::vector<NestedSchema> children;
};

static NestedSchema parse_schema_node(const std::string& token) {
    NestedSchema node;
    size_t brace = token.find('{');

    if (brace == std::string::npos) {
        size_t bracket = token.find('[');
        if (bracket != std::string::npos) {
            node.name = token.substr(0, bracket);
            node.kind = NestedSchema::ARRAY;
        } else {
            node.name = token;
            node.kind = NestedSchema::SCALAR;
        }
        return node;
    }

    node.name = token.substr(0, brace);
    node.kind = NestedSchema::OBJECT;
    size_t close = find_matching_brace(token, brace);
    std::string inside = token.substr(brace + 1, close - brace - 1);

    if (!inside.empty()) {
        for (const std::string& child : split_top_level(inside, ',')) {
            node.children.push_back(parse_schema_node(child));
        }
    }
    return node;
}

static std::vector<NestedSchema> parse_nested_schema(const std::string& header) {
    size_t lb = header.find('[');
    size_t rb = header.find(']');
    if (lb == std::string::npos || rb == std::string::npos) {
        throw std::runtime_error("decode_nested: malformed header");
    }
    size_t open = header.find('{', rb);
    if (open == std::string::npos) {
        throw std::runtime_error("decode_nested: missing schema");
    }
    size_t close = find_matching_brace(header, open);
    std::string schema_text = header.substr(open + 1, close - open - 1);
    std::vector<NestedSchema> schema;
    if (!schema_text.empty()) {
        for (const std::string& token : split_top_level(schema_text, ',')) {
            schema.push_back(parse_schema_node(token));
        }
    }
    return schema;
}

static size_t parse_nested_value(const std::string& text, size_t pos, const NestedSchema& schema, py::object& result) {
    if (pos >= text.size()) {
        throw std::runtime_error("decode_nested: unexpected end of row");
    }

    if (schema.kind == NestedSchema::SCALAR) {
        size_t end = pos;
        bool quoted = false;
        if (text[pos] == '"') {
            quoted = true;
            ++end;
            bool escaped = false;
            while (end < text.size()) {
                char c = text[end];
                if (escaped) {
                    escaped = false;
                } else if (c == '\\') {
                    escaped = true;
                } else if (c == '"') {
                    ++end;
                    break;
                }
                ++end;
            }
        } else {
            while (end < text.size() && text[end] != ',') {
                ++end;
            }
        }
        std::string raw = text.substr(pos, end - pos);
        if (quoted) {
            auto parsed = split_row_typed(raw);
            if (parsed.empty()) {
                result = py::str("");
            } else {
                result = py::str(parsed[0].first);
            }
        } else {
            result = type_field(raw, false);
        }
        return end;
    }

    if (schema.kind == NestedSchema::OBJECT) {
        if (text.compare(pos, 4, "null") == 0) {
            result = py::none();
            return pos + 4;
        }
        if (text[pos] != '{') {
            throw std::runtime_error("decode_nested: expected '{' for object '" + schema.name + "'");
        }
        size_t close = find_matching_brace(text, pos);
        std::string inside = text.substr(pos + 1, close - pos - 1);
        auto values = split_top_level(inside, ',');
        if (values.size() != schema.children.size()) {
            throw std::runtime_error("decode_nested: object field count mismatch for '" + schema.name + "'");
        }
        py::dict obj;
        for (size_t i = 0; i < schema.children.size(); ++i) {
            py::object child;
            parse_nested_value(values[i], 0, schema.children[i], child);
            obj[py::str(schema.children[i].name)] = child;
        }
        result = obj;
        return close + 1;
    }

    if (text.compare(pos, 4, "null") == 0) {
        result = py::none();
        return pos + 4;
    }
    if (text[pos] != '[') {
        throw std::runtime_error("decode_nested: expected '[' for array '" + schema.name + "'");
    }
    size_t close_bracket = text.find(']', pos);
    if (close_bracket == std::string::npos) {
        throw std::runtime_error("decode_nested: malformed array");
    }
    long count = std::stol(text.substr(pos + 1, close_bracket - pos - 1));
    if (close_bracket + 1 >= text.size() || text[close_bracket + 1] != '{') {
        throw std::runtime_error("decode_nested: expected '{' after array length");
    }
    size_t close = find_matching_brace(text, close_bracket + 1);
    std::string inside = text.substr(close_bracket + 2, close - close_bracket - 2);
    py::list arr;
    if (count > 0) {
        auto items = split_top_level(inside, ',');
        if ((long)items.size() != count) {
            throw std::runtime_error("decode_nested: array length mismatch");
        }
        for (const std::string& item : items) {
            auto parsed = split_row_typed(item);
            if (parsed.empty()) {
                arr.append(py::str(""));
            } else {
                arr.append(type_field(parsed[0].first, parsed[0].second));
            }
        }
    }
    result = arr;
    return close + 1;
}

py::list decode_nested(const std::string& text) {
    std::istringstream stream(text);
    std::string header;
    if (!std::getline(stream, header)) {
        throw std::runtime_error("decode_nested: empty input");
    }
    size_t lb = header.find('[');
    size_t rb = header.find(']');
    if (lb == std::string::npos || rb == std::string::npos) {
        throw std::runtime_error("decode_nested: malformed header");
    }
    long n = std::stol(header.substr(lb + 1, rb - lb - 1));
    std::vector<NestedSchema> schema = parse_nested_schema(header);
    std::string line;
    py::list rows;
    for (long r = 0; r < n; ++r) {
        if (!std::getline(stream, line)) {
            throw std::runtime_error("decode_nested: expected " + std::to_string(n) + " rows, stream ended early");
        }
        size_t start = 0;
        while (start < line.size() && line[start] == ' ') {
            ++start;
        }
        std::string content = line.substr(start);
        if (content.empty()) {
            throw std::runtime_error("decode_nested: empty row");
        }
        auto values = split_top_level(content, ',');
        if (values.size() != schema.size()) {
            throw std::runtime_error("decode_nested: row " + std::to_string(r) + " has " + std::to_string(values.size()) + " fields, expected " + std::to_string(schema.size()));
        }
        py::dict row;
        for (size_t i = 0; i < schema.size(); ++i) {
            py::object value;
            parse_nested_value(values[i], 0, schema[i], value);
            row[py::str(schema[i].name)] = value;
        }
        rows.append(row);
    }
    return rows;
}

PYBIND11_MODULE(toon_cpp, m) {
    m.doc() = "C++ TOON encoder/decoder with flat and schema-hoisted recursive nested formats.";
    m.def("encode_flat", &encode_flat, "Encode a list of uniform dict rows to TOON flat/tabular text");
    m.def("decode_flat", &decode_flat, "Decode TOON flat/tabular text back to a list of dict rows");
    m.def("encode_nested", &encode_nested, "Encode a list of nested dict rows to TOON nested-block text");
    m.def("decode_nested", &decode_nested, "Decode TOON nested-block text back to a list of dict rows");
}