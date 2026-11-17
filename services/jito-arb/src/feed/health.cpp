#include "feed/health.hpp"

namespace arb::feed {

FeedEvent FeedHealth::update(const FeedHealthSample& s) {
  hist_.emplace_back(s.now_ms, s.reconnects_total);
  while (hist_.size() > 1 && s.now_ms - hist_.front().first > 60'000) hist_.pop_front();
  per_min_ = int(s.reconnects_total - hist_.front().second);

  if (s.shards_total > 0 && s.shards_up < s.shards_total)
    reason_ = std::to_string(s.shards_total - s.shards_up) + " of " + std::to_string(s.shards_total) + " streams down";
  else if (per_min_ >= c_.flap_reconnects_per_min)
    reason_ = "streams flapping: " + std::to_string(per_min_) + " reconnects in 60 s";
  else if (s.update_age_ms < 0)
    reason_ = "no account update yet";
  else if (s.update_age_ms > c_.max_update_age_ms)
    reason_ = "newest update " + std::to_string(s.update_age_ms) + " ms old";
  else
    reason_.clear();
  ok_ = reason_.empty();

  if (ok_) {
    bad_since_ = -1;
    if (good_since_ < 0) good_since_ = s.now_ms;
    if (alerted_ && s.now_ms - good_since_ >= c_.recover_after_ms) {
      alerted_ = false;
      return FeedEvent::recovered;
    }
    return FeedEvent::none;
  }
  good_since_ = -1;
  if (bad_since_ < 0) bad_since_ = s.now_ms;
  if (!alerted_ && s.now_ms - bad_since_ >= c_.alert_after_ms) {
    alerted_ = true;
    last_alert_ = s.now_ms;
    return FeedEvent::down;
  }
  if (alerted_ && s.now_ms - last_alert_ >= c_.repeat_ms) {
    last_alert_ = s.now_ms;
    return FeedEvent::still_down;
  }
  return FeedEvent::none;
}

}  // namespace arb::feed
