#include "obs/alert.hpp"

#include <chrono>
#include <cstdio>
#include <cstdlib>

#include "obs/log.hpp"
#include "submit/conn.hpp"

namespace arb::obs {

std::string telegram_body(const std::string& chat_id, const std::string& text) {
  std::string out = "{\"chat_id\":\"";
  auto esc = [&out](const std::string& s) {
    for (unsigned char c : s) {
      switch (c) {
        case '"': out += "\\\""; break;
        case '\\': out += "\\\\"; break;
        case '\n': out += "\\n"; break;
        case '\r': break;
        case '\t': out += "\\t"; break;
        default:
          if (c < 0x20) {
            char b[8];
            std::snprintf(b, sizeof b, "\\u%04x", c);
            out += b;
          } else {
            out += char(c);
          }
      }
    }
  };
  esc(chat_id);
  out += "\",\"text\":\"";
  esc(text);
  out += "\",\"disable_web_page_preview\":true}";
  return out;
}

Alerter::Alerter(std::string host) : host_(std::move(host)) {
  if (const char* t = std::getenv("TELEGRAM_BOT_TOKEN")) token_ = t;
  if (const char* c = std::getenv("TELEGRAM_CHAT_ID")) chat_ = c;
  if (enabled()) th_ = std::thread([this] { run(); });
  else LOG_INFO("alerts: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set, alerts go to the log only");
}

Alerter::~Alerter() {
  {
    std::lock_guard<std::mutex> g(mu_);
    stop_ = true;
  }
  cv_.notify_all();
  if (th_.joinable()) th_.join();
}

void Alerter::send(const std::string& text) {
  LOG_WARN("ALERT: %s", text.c_str());
  if (!enabled()) return;
  {
    std::lock_guard<std::mutex> g(mu_);
    if (q_.size() >= 50) q_.pop_front();
    q_.push_back(host_ + ": " + text);
  }
  cv_.notify_one();
}

void Alerter::run() {
  for (;;) {
    std::string text;
    {
      std::unique_lock<std::mutex> l(mu_);
      cv_.wait(l, [this] { return stop_ || !q_.empty(); });
      if (q_.empty()) return;  // stop with nothing left: queued alerts are flushed first
      text = std::move(q_.front());
      q_.pop_front();
    }
    if (post(text)) sent_.fetch_add(1);
    else failed_.fetch_add(1);
    std::this_thread::sleep_for(std::chrono::seconds(2));  // Telegram allows ~1 message/s per chat
  }
}

bool Alerter::post(const std::string& text) {
  std::string out, err;
  int status = http_once("POST", "https://api.telegram.org/bot" + token_ + "/sendMessage", telegram_body(chat_, text), 5000, out, &err);
  if (status == 200) return true;
  // Never log the URL: it carries the bot token.
  if (status == 0) LOG_WARN("alerts: telegram unreachable: %s", err.c_str());
  else LOG_WARN("alerts: telegram http %d: %s", status, out.substr(0, 200).c_str());
  return false;
}

}  // namespace arb::obs
