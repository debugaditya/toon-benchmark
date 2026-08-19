/*
 * DEPRECATED -- superseded by toon_cpp.cpp in this same directory.
 *
 * This earlier draft had two problems, found during verification against
 * the real official toon-format package:
 *   1. It was schema-hardcoded to id/name/age/city specifically, not a
 *      general codec (field names should come from the data).
 *   2. It used CSV-style doubled-quote escaping ("" to escape a literal
 *      quote), but the real spec/official codec uses backslash escaping
 *      (\" \\ \n) -- so this file's output was not actually valid,
 *      interoperable TOON text.
 *
 * toon_cpp.cpp fixes both: field names are read from the data, and the
 * escaping was reverse-engineered directly from the official codec's
 * real output (see codec_comparison.py), then verified byte-identical
 * to it on the full 100k-record dataset and a battery of edge cases.
 *
 * This file is no longer built (setup.py now points at toon_cpp.cpp) --
 * safe to delete via `git rm toon_cpp/toon_native.cpp`.
 */
