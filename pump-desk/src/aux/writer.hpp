#pragma once
// Logger (any non-hot thread) and the writer thread: drains the logic event
// queue into SQLite (WAL) and a JSONL file, keeps the `positions` table that
// boot restores from, and prints a status line.
#include <cstdio>
#include <mutex>
#include <string>
#include <vector>

#include "core/config.hpp"
#include "logic/engine.hpp"
#include "logic/shared.hpp"

struct sqlite3;
struct sqlite3_stmt;

namespace pd {

class Logger {
 public:
  void info(const std::string& m) { line("INFO", m); }
  void warn(const std::string& m) { line("WARN", m); }
  void error(const std::string& m) { line("ERROR", m); }

 private:
  void line(const char* level, const std::string& m);
  std::mutex mu_;
};

/** Event -> JSON object (one JSONL line, no trailing newline). */
std::string event_json(const Event& e);
const char* event_name(Ev t);

class Writer {
 public:
  Writer(const Config& cfg, Shared& sh, Logger& log);
  ~Writer();
  /** Open the database and JSONL file (creating directories' files, not directories). Throws. */
  void open();
  /** Open positions recorded by a previous run. */
  std::vector<RestoredPosition> open_positions();
  /** Mark a restored position closed without trading (its tokens are gone). */
  void close_orphan(const Pubkey& mint, const char* reason);
  /** Writer thread loop; returns once sh.stop is set and the queue is drained. */
  void run();
  /** Write one event now (tests, boot). */
  void write(const Event& e);

 private:
  void exec(const char* sql);
  void status_line(const Event& e);

  const Config& cfg_;
  Shared& sh_;
  Logger& log_;
  sqlite3* db_ = nullptr;
  sqlite3_stmt* ins_event_ = nullptr;
  sqlite3_stmt* upsert_open_ = nullptr;
  sqlite3_stmt* upsert_pending_ = nullptr;
  sqlite3_stmt* update_pos_ = nullptr;
  sqlite3_stmt* close_pos_ = nullptr;
  FILE* jsonl_ = nullptr;
};

}  // namespace pd
