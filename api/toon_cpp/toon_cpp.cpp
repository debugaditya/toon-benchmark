// C++ implementation of the flat/tabular TOON encode/decode path.
// Optimized version: preserves the existing flat TOON behavior while
// minimizing Python-object lookups, temporary strings, and stream overhead
// in the encoder.
//
// Scope: FLAT/uniform-record arrays only.
// Nested structures continue to use the official pure-Python codec.
//
// The encoder keeps the same public API:
//     toon_cpp.encode_flat(rows)
//     toon_cpp.decode_flat(text)
//
// Important: TOON output semantics are intentionally kept equivalent to the
// previous implementation. The optimization is implementation-level only.

#include <pybind11/pybind11.h>
#include <pybind11/stl.h>
#include <string>
#include <vector>
#include <sstream>
#include <cctype>
#include <stdexcept>
#include <Python.h>

namespace py = pybind11;

// ---------- helpers ----------

static bool looks_like_number(const std::string& s) {
    if (s.empty()) return false;

    size_t i = 0;
    if (s[i] == '-' || s[i] == '+') ++i;
    if (i >= s.size()) return false;

    bool seen_digit = false;
    bool seen_dot = false;

    for (; i < s.size(); ++i) {
        const unsigned char c = static_cast<unsigned char>(s[i]);

        if (std::isdigit(c)) {
            seen_digit = true;
            continue;
        }

        if (s[i] == '.' && !seen_dot) {
            seen_dot = true;
            continue;
        }

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
        if (c == ',' || c == '"' || c == '\\' || c == '\n')
            return true;
    }

    return false;
}

// Append a Python string using exactly the same quoting/escaping rules as
// the previous implementation, but without creating temporary strings.
static inline void append_string(std::string& out, const std::string& s) {
    if (!needs_quoting(s)) {
        out.append(s);
        return;
    }

    out.push_back('"');

    for (char c : s) {
        switch (c) {
            case '\\':
                out.append("\\\\");
                break;
            case '"':
                out.append("\\\"");
                break;
            case '\n':
                out.append("\\n");
                break;
            default:
                out.push_back(c);
                break;
        }
    }

    out.push_back('"');
}

// Append one already-known Python scalar directly into the output buffer.
// This avoids the old encode_scalar() temporary std::string on every value.
static inline void append_scalar(std::string& out, const py::handle& v) {
    if (v.is_none()) {
        out.append("null");
        return;
    }

    // Keep bool before int because bool is a Python int subclass.
    if (PyBool_Check(v.ptr())) {
        out.append(PyObject_IsTrue(v.ptr()) ? "true" : "false");
        return;
    }

    if (PyLong_Check(v.ptr())) {
        const long long value = PyLong_AsLongLong(v.ptr());

        if (!PyErr_Occurred()) {
            out.append(std::to_string(value));
            return;
        }

        // Preserve the old behavior for values that cannot fit in long long:
        // clear the Python conversion error and fall through to the same
        // unsupported-scalar error rather than silently changing semantics.
        PyErr_Clear();

        throw std::runtime_error(
            "encode_flat: unsupported integer outside signed 64-bit range"
        );
    }

    if (PyFloat_Check(v.ptr())) {
        // Keep stream-based float formatting for semantic compatibility with
        // the previous implementation. The major optimization is that the
        // result is appended directly rather than returned as a temporary
        // std::string.
        std::ostringstream oss;
        oss << PyFloat_AS_DOUBLE(v.ptr());
        out.append(oss.str());
        return;
    }

    if (PyUnicode_Check(v.ptr())) {
        // pybind11 conversion is retained for Unicode correctness and to
        // preserve the previous UTF-8 string behavior.
        const std::string s = py::reinterpret_borrow<py::str>(v).cast<std::string>();
        append_string(out, s);
        return;
    }

    throw std::runtime_error(
        "encode_flat: unsupported scalar type in flat row "
        "(only str/int/float/bool/None supported)"
    );
}

// ---------- encode ----------

std::string encode_flat(const py::list& rows) {
    const py::ssize_t row_count = py::len(rows);

    if (row_count == 0) {
        return "[0]{}:";
    }

    // Extract field names once.
    py::dict first = py::reinterpret_borrow<py::dict>(rows[0]);

    std::vector<std::string> fields;
    std::vector<py::str> keys;

    fields.reserve(first.size());
    keys.reserve(first.size());

    for (auto item : first) {
        py::str key = py::reinterpret_borrow<py::str>(item.first);
        keys.push_back(key);
        fields.push_back(key.cast<std::string>());
    }

    // Rough reservation. This is intentionally conservative: it reduces
    // reallocations without requiring a costly pre-pass over all values.
    size_t estimated = 32 + fields.size() * 16;
    estimated += static_cast<size_t>(row_count) *
                 (2 + fields.size() * 8);
    std::string out;
    out.reserve(estimated);

    out.push_back('[');
    out.append(std::to_string(row_count));
    out.append("]{");

    for (size_t i = 0; i < fields.size(); ++i) {
        if (i) out.push_back(',');
        out.append(fields[i]);
    }

    out.append("}:");

    for (auto row_h : rows) {
        // Avoid an additional cast through py::dict construction.
        PyObject* row_obj = row_h.ptr();

        if (!PyDict_Check(row_obj)) {
            throw std::runtime_error(
                "encode_flat: every row must be a dict"
            );
        }

        out.append("\n  ");

        for (size_t i = 0; i < fields.size(); ++i) {
            if (i) out.push_back(',');

            // One native Python dictionary lookup instead of:
            //     row.contains(...)
            //     row[py::str(fields[i])]
            //
            // The cached key object is reused for every row.
            PyObject* value = PyDict_GetItemWithError(
                row_obj,
                keys[i].ptr()
            );

            if (!value) {
                if (PyErr_Occurred())
                    throw py::error_already_set();

                throw std::runtime_error(
                    "encode_flat: row missing field '" +
                    fields[i] +
                    "' -- all rows must share the same keys"
                );
            }

            append_scalar(out, py::handle(value));
        }
    }

    return out;
}

// ---------- decode ----------

static py::object type_field(const std::string& raw, bool was_quoted) {
    if (was_quoted) return py::str(raw);

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
            return py::float_(std::stod(raw));
        }
    }

    return py::str(raw);
}

static std::vector<std::pair<std::string, bool>>
split_row_typed(const std::string& line) {
    std::vector<std::pair<std::string, bool>> fields;
    fields.reserve(8);

    size_t i = 0;
    const size_t n = line.size();

    while (i <= n) {
        std::string field;
        bool quoted = false;

        if (i < n && line[i] == '"') {
            quoted = true;
            ++i;

            while (i < n) {
                const char c = line[i];

                if (c == '\\' && i + 1 < n) {
                    const char nc = line[i + 1];

                    if (nc == '\\') {
                        field.push_back('\\');
                        i += 2;
                        continue;
                    }

                    if (nc == '"') {
                        field.push_back('"');
                        i += 2;
                        continue;
                    }

                    if (nc == 'n') {
                        field.push_back('\n');
                        i += 2;
                        continue;
                    }

                    field.push_back(c);
                    ++i;
                    continue;
                }

                if (c == '"') {
                    ++i;
                    break;
                }

                field.push_back(c);
                ++i;
            }
        } else {
            while (i < n && line[i] != ',') {
                field.push_back(line[i]);
                ++i;
            }
        }

        fields.emplace_back(std::move(field), quoted);

        if (i < n && line[i] == ',') {
            ++i;
            continue;
        }

        break;
    }

    return fields;
}

py::list decode_flat(const std::string& text) {
    std::istringstream stream(text);
    std::string header;

    if (!std::getline(stream, header)) {
        throw std::runtime_error(
            "decode_flat: malformed header, expected '[N]{fields}:'"
        );
    }

    const size_t lb = header.find('[');
    const size_t rb = header.find(']');
    const size_t cb = header.find('{');
    const size_t cbe = header.find('}');

    if (lb == std::string::npos || rb == std::string::npos ||
        cb == std::string::npos || cbe == std::string::npos) {
        throw std::runtime_error(
            "decode_flat: malformed header, expected '[N]{fields}:'"
        );
    }

    const long n = std::stol(
        header.substr(lb + 1, rb - lb - 1)
    );

    const std::string field_str =
        header.substr(cb + 1, cbe - cb - 1);

    std::vector<std::string> fields;

    if (!field_str.empty()) {
        std::istringstream fs(field_str);
        std::string f;

        while (std::getline(fs, f, ',')) {
            fields.push_back(std::move(f));
        }
    }

    py::list rows;

    std::string line;

    for (long r = 0; r < n; ++r) {
        if (!std::getline(stream, line)) {
            throw std::runtime_error(
                "decode_flat: expected " +
                std::to_string(n) +
                " rows, stream ended early"
            );
        }

        size_t start = 0;

        while (start < line.size() && line[start] == ' ')
            ++start;

        std::string content = line.substr(start);

        auto typed_fields = split_row_typed(content);

        if (typed_fields.size() != fields.size()) {
            throw std::runtime_error(
                "decode_flat: row " +
                std::to_string(r) +
                " has " +
                std::to_string(typed_fields.size()) +
                " fields, expected " +
                std::to_string(fields.size())
            );
        }

        py::dict row;

        for (size_t i = 0; i < fields.size(); ++i) {
            row[py::str(fields[i])] =
                type_field(
                    typed_fields[i].first,
                    typed_fields[i].second
                );
        }

        rows.append(std::move(row));
    }

    return rows;
}

PYBIND11_MODULE(toon_cpp, m) {
    m.doc() =
        "Optimized C++ implementation of the flat/tabular TOON "
        "encode/decode path.";

    m.def(
        "encode_flat",
        &encode_flat,
        "Encode a list of uniform dict rows to TOON flat/tabular text"
    );

    m.def(
        "decode_flat",
        &decode_flat,
        "Decode TOON flat/tabular text back to a list of dict rows"
    );
}