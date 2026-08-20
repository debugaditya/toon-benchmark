// Recursive C++ TOON encoder for JSON-shaped Python data.
//
// API:
//   toon_cpp.encode_toon(value)  -> str
//   toon_cpp.encode_flat(rows)   -> str (kept as compatibility alias)
//
// The encoder recursively handles:
//   - primitives
//   - objects/dicts
//   - primitive arrays (inline)
//   - arrays of uniform objects (tabular)
//   - arrays of nested-uniform objects (nested field groups)
//   - mixed/non-uniform arrays (list form)
//   - nested arrays/objects
//
// Default delimiter is comma. Indentation is two spaces.
// This targets TOON spec 4.1 behavior for the JSON-shaped values used by the
// benchmark. The existing decode_flat API is intentionally retained separately.

#include <pybind11/pybind11.h>
#include <pybind11/stl.h>
#include <string>
#include <vector>
#include <sstream>
#include <cctype>
#include <stdexcept>
#include <algorithm>
#include <functional>
#include <cstdio>
#include <Python.h>

namespace py = pybind11;

static constexpr char DELIM = ',';
static constexpr int INDENT = 2;

// -----------------------------------------------------------------------------
// Scalar helpers
// -----------------------------------------------------------------------------

static bool looks_like_number(const std::string& s) {
    if (s.empty()) return false;
    size_t i = 0;
    if (s[i] == '-' || s[i] == '+') ++i;
    if (i >= s.size()) return false;

    bool digit = false, dot = false;
    for (; i < s.size(); ++i) {
        unsigned char c = static_cast<unsigned char>(s[i]);
        if (std::isdigit(c)) { digit = true; continue; }
        if (s[i] == '.' && !dot) { dot = true; continue; }
        return false;
    }
    return digit;
}

static bool needs_quoting(const std::string& s) {
    if (s.empty()) return true;
    if (s.front() == ' ' || s.back() == ' ') return true;
    if (s == "true" || s == "false" || s == "null") return true;
    if (looks_like_number(s)) return true;
    for (char c : s) {
        if (c == DELIM || c == '"' || c == '\\' || c == '\n' ||
            c == '\r' || c == '\t') return true;
        if (static_cast<unsigned char>(c) < 0x20) return true;
    }
    return false;
}

static void append_quoted(std::string& out, const std::string& s) {
    out.push_back('"');
    for (char c : s) {
        switch (c) {
            case '\\': out.append("\\\\"); break;
            case '"':  out.append("\\\""); break;
            case '\n': out.append("\\n"); break;
            case '\r': out.append("\\r"); break;
            case '\t': out.append("\\t"); break;
            default:
                if (static_cast<unsigned char>(c) < 0x20) {
                    // Conservative fallback for control characters.
                    char buf[7];
                    std::snprintf(buf, sizeof(buf), "\\u%04x",
                                  static_cast<unsigned char>(c));
                    out.append(buf);
                } else {
                    out.push_back(c);
                }
        }
    }
    out.push_back('"');
}

static void append_string(std::string& out, const std::string& s) {
    if (needs_quoting(s)) append_quoted(out, s);
    else out.append(s);
}

static bool is_scalar(const py::handle& v) {
    return v.is_none() || PyBool_Check(v.ptr()) || PyLong_Check(v.ptr()) ||
           PyFloat_Check(v.ptr()) || PyUnicode_Check(v.ptr());
}

static void append_scalar(std::string& out, const py::handle& v) {
    if (v.is_none()) { out.append("null"); return; }

    if (PyBool_Check(v.ptr())) {
        out.append(PyObject_IsTrue(v.ptr()) ? "true" : "false");
        return;
    }

    if (PyLong_Check(v.ptr())) {
        long long x = PyLong_AsLongLong(v.ptr());
        if (!PyErr_Occurred()) {
            out.append(std::to_string(x));
            return;
        }
        PyErr_Clear();
        throw std::runtime_error("toon_cpp: integer outside signed 64-bit range");
    }

    if (PyFloat_Check(v.ptr())) {
        std::ostringstream oss;
        oss << PyFloat_AS_DOUBLE(v.ptr());
        out.append(oss.str());
        return;
    }

    if (PyUnicode_Check(v.ptr())) {
        append_string(out, py::reinterpret_borrow<py::str>(v).cast<std::string>());
        return;
    }

    throw std::runtime_error("toon_cpp: unsupported scalar type");
}

static std::string key_to_string(const py::handle& key) {
    if (!PyUnicode_Check(key.ptr()))
        throw std::runtime_error("toon_cpp: object keys must be strings");
    return key.cast<std::string>();
}

static void append_indent(std::string& out, int depth) {
    out.append(static_cast<size_t>(depth * INDENT), ' ');
}

// -----------------------------------------------------------------------------
// Shape detection for tabular arrays
// -----------------------------------------------------------------------------

struct FieldInfo {
    std::string name;
    bool nested = false;
    std::vector<FieldInfo> children;
};

static bool get_dict_keys(const py::dict& d, std::vector<std::string>& keys) {
    keys.clear();
    keys.reserve(d.size());
    for (auto item : d) keys.push_back(key_to_string(item.first));
    return true;
}

static bool same_key_set(const py::dict& a, const py::dict& b) {
    if (a.size() != b.size()) return false;
    for (auto item : a) {
        if (!b.contains(item.first)) return false;
    }
    return true;
}

// Recursive shape helpers are retained for compatibility, but tabular output
// is intentionally restricted to flat object arrays for this benchmark.
static bool build_uniform_object_shape(const py::dict& first,
                                       const py::list& objects,
                                       std::vector<FieldInfo>& shape) {
    if (first.size() == 0) return false;
    shape.clear();

    for (auto first_item : first) {
        const std::string name = key_to_string(first_item.first);
        FieldInfo fi;
        fi.name = name;

        bool all_nested_objects = true;
        bool all_scalars = true;
        py::handle first_value = first_item.second;

        if (!PyDict_Check(first_value.ptr())) all_nested_objects = false;
        if (!is_scalar(first_value)) all_scalars = false;

        if (all_nested_objects) {
            py::dict first_nested = py::reinterpret_borrow<py::dict>(first_value);
            if (first_nested.size() == 0) return false;

            // Check every row's corresponding value.
            for (auto row_h : objects) {
                py::dict row = py::reinterpret_borrow<py::dict>(row_h);
                py::handle v = row[py::reinterpret_borrow<py::str>(first_item.first)];
                if (!PyDict_Check(v.ptr())) { all_nested_objects = false; break; }
                py::dict nd = py::reinterpret_borrow<py::dict>(v);
                if (nd.size() == 0 || !same_key_set(first_nested, nd)) {
                    all_nested_objects = false; break;
                }
            }

            if (all_nested_objects) {
                // Recursively validate the nested object column.
                py::list nested_objects;
                for (auto row_h : objects) {
                    py::dict row = py::reinterpret_borrow<py::dict>(row_h);
                    nested_objects.append(row[py::reinterpret_borrow<py::str>(first_item.first)]);
                }
                if (!build_uniform_object_shape(first_nested, nested_objects, fi.children))
                    return false;
                fi.nested = true;
            }
        }

        if (!fi.nested) {
            // Primitive column: every value must be scalar.
            if (!all_scalars) return false;
            for (auto row_h : objects) {
                py::dict row = py::reinterpret_borrow<py::dict>(row_h);
                py::handle v = row[py::reinterpret_borrow<py::str>(first_item.first)];
                if (!is_scalar(v)) return false;
            }
        }

        shape.push_back(std::move(fi));
    }
    return true;
}

// Tabular TOON is used ONLY when every object in the array is flat:
// every field value is a scalar. If an object contains a dict/list, the
// official nested/list representation is preserved instead.
static bool is_flat_uniform_object_array(const py::list& arr,
                                         std::vector<FieldInfo>& shape) {
    if (arr.size() == 0) return false;

    py::handle first_h = arr[0];
    if (!PyDict_Check(first_h.ptr())) return false;

    py::dict first = py::reinterpret_borrow<py::dict>(first_h);
    if (first.size() == 0) return false;

    for (auto item : first) {
        if (!is_scalar(item.second))
            return false;
    }

    for (auto h : arr) {
        if (!PyDict_Check(h.ptr())) return false;

        py::dict d = py::reinterpret_borrow<py::dict>(h);
        if (d.size() == 0 || !same_key_set(first, d))
            return false;

        for (auto item : d) {
            if (!is_scalar(item.second))
                return false;
        }
    }

    shape.clear();
    for (auto item : first) {
        FieldInfo fi;
        fi.name = key_to_string(item.first);
        fi.nested = false;
        shape.push_back(std::move(fi));
    }

    return true;
}

static int leaf_count(const std::vector<FieldInfo>& shape) {
    int n = 0;
    for (const auto& f : shape)
        n += f.nested ? leaf_count(f.children) : 1;
    return n;
}

static void append_field_group(std::string& out, const FieldInfo& f) {
    append_string(out, f.name);
    if (f.nested) {
        out.push_back('{');
        for (size_t i = 0; i < f.children.size(); ++i) {
            if (i) out.push_back(DELIM);
            append_field_group(out, f.children[i]);
        }
        out.push_back('}');
    }
}

// -----------------------------------------------------------------------------
// Recursive encoding
// -----------------------------------------------------------------------------

static void append_primitive_array_inline(std::string& out, const py::list& arr) {
    out.push_back('[');
    out.append(std::to_string(arr.size()));
    out.append("]: ");
    for (size_t i = 0; i < static_cast<size_t>(arr.size()); ++i) {
        if (i) out.push_back(DELIM);
        if (!is_scalar(arr[i]))
            throw std::runtime_error("toon_cpp: primitive array contains non-primitive value");
        append_scalar(out, arr[i]);
    }
}

static void append_object_fields(std::string& out, const py::dict& obj, int depth);
static void append_array_field(std::string& out, const std::string& key,
                               const py::list& arr, int depth);

static void append_tabular_rows(std::string& out, const py::list& arr,
                                const std::vector<FieldInfo>& shape, int depth) {
    for (auto row_h : arr) {
        py::dict row = py::reinterpret_borrow<py::dict>(row_h);
        append_indent(out, depth);
        bool first_leaf = true;

        std::function<void(const std::vector<FieldInfo>&, const py::dict&)> emit =
            [&](const std::vector<FieldInfo>& fs, const py::dict& d) {
                for (const auto& f : fs) {
                    py::str k(f.name);
                    py::handle v = d[k];
                    if (f.nested) {
                        py::dict nd = py::reinterpret_borrow<py::dict>(v);
                        emit(f.children, nd);
                    } else {
                        if (!first_leaf) out.push_back(DELIM);
                        append_scalar(out, v);
                        first_leaf = false;
                    }
                }
            };

        emit(shape, row);
        out.push_back('\n');
    }
}

static void append_array_value(std::string& out, const py::list& arr, int depth,
                               bool keyed = false, const std::string& key = "") {
    if (arr.size() == 0) {
        if (!key.empty()) {
            append_indent(out, depth);
            append_string(out, key);
            out.append(": []\n");
        } else {
            append_indent(out, depth);
            out.append("[]\n");
        }
        return;
    }

    // Primitive arrays are inline.
    bool all_scalar = true;
    for (auto v : arr) if (!is_scalar(v)) { all_scalar = false; break; }
    if (all_scalar) {
        append_indent(out, depth);
        if (!key.empty()) {
            append_string(out, key);
        }
        append_primitive_array_inline(out, arr);
        out.push_back('\n');
        return;
    }

    std::vector<FieldInfo> shape;
    if (is_flat_uniform_object_array(arr, shape)) {
        append_indent(out, depth);
        if (!key.empty()) append_string(out, key);
        out.push_back('[');
        out.append(std::to_string(arr.size()));
        out.push_back(']');
        out.push_back('{');
        for (size_t i = 0; i < shape.size(); ++i) {
            if (i) out.push_back(DELIM);
            append_field_group(out, shape[i]);
        }
        out.append(":\n");
        append_tabular_rows(out, arr, shape, depth + 1);
        return;
    }

    // Non-uniform / mixed array: list form.
    append_indent(out, depth);
    if (!key.empty()) append_string(out, key);
    out.push_back('[');
    out.append(std::to_string(arr.size()));
    out.append("]:\n");

    for (auto v : arr) {
        append_indent(out, depth + 1);
        out.append("- ");

        if (is_scalar(v)) {
            append_scalar(out, v);
            out.push_back('\n');
        } else if (PyDict_Check(v.ptr())) {
            py::dict obj = py::reinterpret_borrow<py::dict>(v);
            if (obj.size() == 0) {
                out.push_back('\n');
            } else {
                // First field lives on the hyphen line, remaining fields at depth+1.
                bool first = true;
                for (auto item : obj) {
                    const std::string k = key_to_string(item.first);
                    py::handle val = item.second;
                    if (!first) append_indent(out, depth + 2);
                    append_string(out, k);
                    out.append(":");
                    if (is_scalar(val)) {
                        out.push_back(' ');
                        append_scalar(out, val);
                        out.push_back('\n');
                    } else if (PyDict_Check(val.ptr())) {
                        out.push_back('\n');
                        append_object_fields(out, py::reinterpret_borrow<py::dict>(val), depth + 2);
                    } else if (PyList_Check(val.ptr())) {
                        // The key and colon are already on the current list-item line.
                        py::list arr = py::reinterpret_borrow<py::list>(val);
                        if (arr.size() == 0) {
                            out.append(" []\n");
                        } else {
                            bool primitive = true;
                            for (auto x : arr) if (!is_scalar(x)) { primitive = false; break; }
                            if (primitive) {
                                out.push_back(' ');
                                out.push_back('[');
                                out.append(std::to_string(arr.size()));
                                out.append("]: ");
                                for (size_t j = 0; j < static_cast<size_t>(arr.size()); ++j) {
                                    if (j) out.push_back(DELIM);
                                    append_scalar(out, arr[j]);
                                }
                                out.push_back('\n');
                            } else {
                                out.push_back('\n');
                                append_array_value(out, arr, depth + 2, false, "");
                            }
                        }
                    } else {
                        throw std::runtime_error("toon_cpp: unsupported nested value");
                    }
                    first = false;
                }
            }
        } else if (PyList_Check(v.ptr())) {
            // Nested arrays as list items: header remains on the list-item line.
            py::list nested = py::reinterpret_borrow<py::list>(v);
            if (nested.size() == 0) {
                out.append("[]\n");
            } else {
                bool nested_scalars = true;
                for (auto x : nested) if (!is_scalar(x)) { nested_scalars = false; break; }
                if (nested_scalars) {
                    out.push_back('[');
                    out.append(std::to_string(nested.size()));
                    out.append("]: ");
                    for (size_t j = 0; j < static_cast<size_t>(nested.size()); ++j) {
                        if (j) out.push_back(DELIM);
                        append_scalar(out, nested[j]);
                    }
                    out.push_back('\n');
                } else {
                    out.push_back('[');
                    out.append(std::to_string(nested.size()));
                    out.append("]:\n");
                    // Generic recursive fallback for nested array items.
                    for (auto x : nested) {
                        append_indent(out, depth + 2);
                        out.append("- ");
                        if (is_scalar(x)) append_scalar(out, x);
                        else throw std::runtime_error("toon_cpp: deeply nested mixed arrays require additional list handling");
                        out.push_back('\n');
                    }
                }
            }
        }
    }
}

static void append_array_field(std::string& out, const std::string& key,
                               const py::list& arr, int depth) {
    append_array_value(out, arr, depth, true, key);
}

static void append_object_fields(std::string& out, const py::dict& obj, int depth) {
    for (auto item : obj) {
        const std::string key = key_to_string(item.first);
        py::handle value = item.second;

        append_indent(out, depth);
        append_string(out, key);

        if (is_scalar(value)) {
            out.append(": ");
            append_scalar(out, value);
            out.push_back('\n');
        } else if (PyDict_Check(value.ptr())) {
            py::dict child = py::reinterpret_borrow<py::dict>(value);
            out.append(":\n");
            if (child.size() == 0) continue;
            append_object_fields(out, child, depth + 1);
        } else if (PyList_Check(value.ptr())) {
            py::list arr = py::reinterpret_borrow<py::list>(value);
            append_array_field(out, key, arr, depth);
        } else {
            throw std::runtime_error("toon_cpp: unsupported object value type");
        }
    }
}

static std::string encode_toon(const py::handle& value) {
    std::string out;
    out.reserve(1024);

    if (is_scalar(value)) {
        append_scalar(out, value);
        return out;
    }

    if (PyDict_Check(value.ptr())) {
        py::dict obj = py::reinterpret_borrow<py::dict>(value);
        append_object_fields(out, obj, 0);
        if (!out.empty() && out.back() == '\n') out.pop_back();
        return out;
    }

    if (PyList_Check(value.ptr())) {
        py::list arr = py::reinterpret_borrow<py::list>(value);
        append_array_value(out, arr, 0, false, "");
        if (!out.empty() && out.back() == '\n') out.pop_back();
        return out;
    }

    throw std::runtime_error("toon_cpp: root must be a JSON-compatible scalar, object, or array");
}

// Compatibility: flat arrays continue to use the same recursive encoder.
static std::string encode_flat(const py::list& rows) {
    return encode_toon(rows);
}

PYBIND11_MODULE(toon_cpp, m) {
    m.doc() = "Recursive C++ TOON encoder for JSON-shaped Python data";
    m.def("encode_toon", &encode_toon,
          "Recursively encode JSON-shaped Python data to TOON");
    m.def("encode_flat", &encode_flat,
          "Compatibility alias for recursively encoding flat/tabular rows");
}