// C++ implementation of the flat/tabular TOON encode/decode path,
// matching the OFFICIAL toon-format package's verified behavior exactly
// (reverse-engineered against toon_format v0.9.0-beta.1 by probing its
// actual output on edge cases -- see codec_comparison.py).
//
// Scope: FLAT/uniform-record arrays only (the well-defined tabular case).
// Nested structures continue to use the official pure-Python codec --
// hand-rolling a correct indented-block parser for arbitrary nesting in
// the time available carries real correctness risk, and getting flat
// right first (with exhaustive cross-validation against the official
// codec) matters more than covering both cases quickly.
//
// This is NOT schema-hardcoded -- field names are read from the data
// (the first row's keys), same as the official codec. This replaces an
// earlier draft that hardcoded id/name/age/city and used CSV-style
// doubled-quote escaping; neither matched the real spec's behavior.
//
// Verified escaping rule (backslash-based, NOT CSV double-quoting):
//   Header: "[N]{field1,field2,...}:"
//   Rows: 2-space indented, comma-separated
//   A value is quoted if it:
//     - contains ',' '"' '\' or a newline
//     - has leading/trailing whitespace
//     - is empty
//     - would be misparsed as int/float/true/false/null if left unquoted
//   Inside quotes, escaping order: '\' -> '\\', '"' -> '\"', '\n' -> "\n"
//
// Types on decode: unquoted true/false/null/numeric literals are typed;
// everything else (including any quoted value) is a Python str.

#include <pybind11/pybind11.h>
#include <pybind11/stl.h>
#include <string>
#include <vector>
#include <sstream>
#include <cctype>
#include <stdexcept>

namespace py = pybind11;

// ---------- helpers: value -> string (encode side) ----------

static bool looks_like_number(const std::string& s) {
    if (s.empty()) return false;
    size_t i = 0;
    if (s[i] == '-' || s[i] == '+') i++;
    if (i >= s.size()) return false;
    bool seen_digit = false, seen_dot = false;
    for (; i < s.size(); i++) {
        if (isdigit((unsigned char)s[i])) { seen_digit = true; continue; }
        if (s[i] == '.' && !seen_dot) { seen_dot = true; continue; }
        return false;
    }
    return seen_digit;
}

static bool needs_quoting(const std::string& s) {
    if (s.empty()) return true;
    if (s.front() == ' ' || s.back() == ' ') return true;
    if (s == "true" || s == "false" || s == "null") return true;
    if (looks_like_number(s)) return true;
    for (char c : s) {
        if (c == ',' || c == '"' || c == '\\' || c == '\n') return true;
    }
    return false;
}

static std::string escape_quoted(const std::string& s) {
    std::string out;
    out.reserve(s.size() + 8);
    for (char c : s) {
        if (c == '\\') out += "\\\\";
        else if (c == '"') out += "\\\"";
        else if (c == '\n') out += "\\n";
        else out += c;
    }
    return out;
}

// Converts one Python object (already known to be a plain scalar: str,
// int, float, bool, or None) into its TOON field-string form.
static std::string encode_scalar(const py::handle& v) {
    if (v.is_none()) return "null";
    if (py::isinstance<py::bool_>(v)) return v.cast<bool>() ? "true" : "false";
    if (py::isinstance<py::int_>(v)) return std::to_string(v.cast<long long>());
    if (py::isinstance<py::float_>(v)) {
        std::ostringstream oss;
        oss << v.cast<double>();
        return oss.str();
    }
    if (py::isinstance<py::str>(v)) {
        std::string s = v.cast<std::string>();
        if (needs_quoting(s)) {
            return "\"" + escape_quoted(s) + "\"";
        }
        return s;
    }
    throw std::runtime_error("encode_flat: unsupported scalar type in flat row (only str/int/float/bool/None supported)");
}

// ---------- encode ----------

std::string encode_flat(const py::list& rows) {
    if (rows.size() == 0) {
        return "[0]{}:";
    }
    // Field order taken from the FIRST row's dict; all rows must share
    // the exact same key set.
    py::dict first = rows[0].cast<py::dict>();
    std::vector<std::string> fields;
    for (auto item : first) {
        fields.push_back(py::str(item.first).cast<std::string>());
    }

    std::ostringstream out;
    out << "[" << rows.size() << "]{";
    for (size_t i = 0; i < fields.size(); i++) {
        if (i) out << ",";
        out << fields[i];
    }
    out << "}:";

    for (auto row_h : rows) {
        py::dict row = row_h.cast<py::dict>();
        out << "\n  ";
        for (size_t i = 0; i < fields.size(); i++) {
            if (i) out << ",";
            if (!row.contains(fields[i])) {
                throw std::runtime_error("encode_flat: row missing field '" + fields[i] + "' -- all rows must share the same keys");
            }
            out << encode_scalar(row[py::str(fields[i])]);
        }
    }
    return out.str();
}

// ---------- decode ----------

static py::object type_field(const std::string& raw, bool was_quoted) {
    if (was_quoted) return py::str(raw);  // quoted values are ALWAYS strings
    if (raw == "true") return py::bool_(true);
    if (raw == "false") return py::bool_(false);
    if (raw == "null") return py::none();
    if (looks_like_number(raw)) {
        if (raw.find('.') != std::string::npos) {
            return py::float_(std::stod(raw));
        }
        try {
            return py::int_(std::stoll(raw));
        } catch (...) {
            return py::float_(std::stod(raw));  // overflow fallback
        }
    }
    return py::str(raw);
}

// Quote-aware field splitter. Reports per-field whether it was quoted
// (needed because a quoted "123" must stay a string, while an unquoted
// 123 must become an int) and unescapes backslash sequences in place.
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

    size_t lb = header.find('[');
    size_t rb = header.find(']');
    size_t cb = header.find('{');
    size_t cbe = header.find('}');
    if (lb == std::string::npos || rb == std::string::npos ||
        cb == std::string::npos || cbe == std::string::npos) {
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

PYBIND11_MODULE(toon_cpp, m) {
    m.doc() = "C++ implementation of the flat/tabular TOON encode/decode path, "
              "matching the official toon-format package's verified behavior.";
    m.def("encode_flat", &encode_flat, "Encode a list of uniform dict rows to TOON flat/tabular text");
    m.def("decode_flat", &decode_flat, "Decode TOON flat/tabular text back to a list of dict rows");
}
