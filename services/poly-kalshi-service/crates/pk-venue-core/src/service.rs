//! Process-local state, recording, and fail-closed controls for venue adapters.

use std::collections::{HashMap, VecDeque};
use std::fs::{File, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};

use chrono::Utc;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

use crate::{Book, MatchDecision, RiskGuard, Series, VenueMode};

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct OrderRecord {
    pub id: String,
    pub client_id: String,
    pub market: String,
    pub lane: String,
    pub side: String,
    pub price: f64,
    pub quantity: f64,
    pub status: String,
    pub created_ms: i64,
    pub updated_ms: i64,
    #[serde(default)]
    pub simulated: bool,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct FillRecord {
    pub id: String,
    pub order_id: String,
    pub market: String,
    pub side: String,
    pub price: f64,
    pub quantity: f64,
    pub fee: f64,
    pub time_ms: i64,
    #[serde(default)]
    pub simulated: bool,
}

/// Open YES/NO inventory for one market, with cash cost basis (price × qty + fees).
#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct Position {
    pub yes_qty: f64,
    pub yes_cost: f64,
    pub no_qty: f64,
    pub no_cost: f64,
}

impl Position {
    pub fn is_flat(&self) -> bool {
        self.yes_qty.abs() <= 1e-9 && self.no_qty.abs() <= 1e-9
    }

    pub fn net(&self) -> f64 {
        self.yes_qty - self.no_qty
    }

    /// Mark-to-market vs the venue's YES mid: YES marks at `mid`, NO at `1 − mid`.
    pub fn marked(&self, yes_mid: f64) -> f64 {
        self.yes_qty * yes_mid + self.no_qty * (1.0 - yes_mid) - self.yes_cost - self.no_cost
    }

    pub fn apply_buy(&mut self, side: &str, price: f64, quantity: f64, fee: f64) {
        if quantity <= 1e-12 {
            return;
        }
        let cost = quantity * price + fee;
        if is_yes_side(side) {
            self.yes_qty += quantity;
            self.yes_cost += cost;
        } else {
            self.no_qty += quantity;
            self.no_cost += cost;
        }
    }

    /// Realise `amount` balanced YES/NO pairs at $1 (a merge). Returns realised $.
    pub fn realise_merge(&mut self, amount: f64) -> f64 {
        let pairs = amount.min(self.yes_qty).min(self.no_qty).max(0.0);
        if pairs <= 1e-12 {
            return 0.0;
        }
        let yes_avg = self.yes_cost / self.yes_qty;
        let no_avg = self.no_cost / self.no_qty;
        self.yes_qty -= pairs;
        self.no_qty -= pairs;
        self.yes_cost = (self.yes_cost - pairs * yes_avg).max(0.0);
        self.no_cost = (self.no_cost - pairs * no_avg).max(0.0);
        pairs * (1.0 - yes_avg - no_avg)
    }

    /// Realise resolution payoffs (YES/NO each 0 or 1) and clear the position.
    pub fn realise_resolution(&mut self, yes_payoff: f64, no_payoff: f64) -> f64 {
        let realised =
            self.yes_qty * yes_payoff + self.no_qty * no_payoff - self.yes_cost - self.no_cost;
        *self = Self::default();
        realised
    }
}

/// What a held position needs to be settled and shown: when its market
/// decides, the Kalshi contract it mirrors, and a readable label.
#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct PositionMeta {
    pub close_ms: Option<i64>,
    pub kalshi_ticker: Option<String>,
    pub label: String,
    pub opened_ms: i64,
}

/// The account's P&L over time, persisted beside the risk state: the classic
/// view a live account has (bankroll, realised by day, fees, settled count,
/// win rate, an equity curve). Shadow mode starts from a simulated bankroll.
#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct PnlBook {
    pub capital: f64,
    pub realised_total: f64,
    pub realised_by_day: std::collections::BTreeMap<String, f64>,
    pub fees_total: f64,
    pub settled: u64,
    pub wins: u64,
    pub losses: u64,
    /// `[t_ms, realised_total, marked]`, one point a minute.
    pub series: VecDeque<(i64, f64, f64)>,
    /// Open positions and their metadata, so a restart keeps what is held
    /// (live, the wallet is the record; shadow has only this).
    #[serde(default)]
    pub open: HashMap<String, Position>,
    #[serde(default)]
    pub open_meta: HashMap<String, PositionMeta>,
    #[serde(skip)]
    path: Option<PathBuf>,
}

impl PnlBook {
    /// Load `path` (or start at `capital`); a changed bankroll setting is kept.
    pub fn open(path: PathBuf, capital: f64) -> Self {
        let mut book: Self =
            std::fs::read(&path).ok().and_then(|b| serde_json::from_slice(&b).ok()).unwrap_or_default();
        book.capital = capital;
        book.path = Some(path);
        book
    }

    pub fn save(&self) {
        let Some(path) = &self.path else { return };
        if let Ok(bytes) = serde_json::to_vec(self) {
            let tmp = path.with_extension("json.tmp");
            if std::fs::write(&tmp, bytes).is_ok() {
                let _ = std::fs::rename(&tmp, path);
            }
        }
    }

    fn realise(&mut self, amount: f64, now: i64) {
        self.realised_total += amount;
        let day = chrono::DateTime::<Utc>::from_timestamp_millis(now).unwrap_or_default().format("%Y-%m-%d").to_string();
        *self.realised_by_day.entry(day).or_default() += amount;
        self.settled += 1;
        if amount > 1e-9 {
            self.wins += 1;
        } else if amount < -1e-9 {
            self.losses += 1;
        }
        self.save();
    }

    /// Append an equity point at most once a minute (keeps three days).
    pub fn sample(&mut self, now: i64, marked: f64) {
        if self.series.back().is_some_and(|(t, _, _)| now - t < 60_000) {
            return;
        }
        bounded_push(&mut self.series, (now, self.realised_total, marked), 3 * 1440);
        self.save();
    }
}

/// YES / buy-side fills (Predict `YES`/`yes`, HIP-4 `YES`/`b`/`buy`).
pub fn is_yes_side(side: &str) -> bool {
    matches!(
        side.trim().to_ascii_lowercase().as_str(),
        "yes" | "y" | "b" | "buy" | "long" | "bid"
    )
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct FeedHealth {
    pub connected: bool,
    pub last_message_ms: i64,
    pub reconnects: u64,
    pub errors: u64,
    pub sequence_gaps: u64,
    pub last_error: Option<String>,
}

#[derive(Debug)]
pub struct AdapterState {
    pub venue: String,
    pub mode: VenueMode,
    pub started_ms: i64,
    pub config_fingerprint: String,
    pub ready: bool,
    pub reconciling: bool,
    pub degraded: Vec<String>,
    pub latched: Vec<String>,
    pub clock_offset_ms: Option<i64>,
    pub feeds: HashMap<String, FeedHealth>,
    pub books: HashMap<String, Book>,
    pub kalshi_fairs: HashMap<String, f64>,
    pub matches: HashMap<String, MatchDecision>,
    pub orders: VecDeque<OrderRecord>,
    pub fills: VecDeque<FillRecord>,
    /// Signed net inventory (YES − NO) per market; kept for strategy gates.
    pub inventory: HashMap<String, f64>,
    /// Cost-basis positions used for marked / realised P&L.
    pub positions: HashMap<String, Position>,
    pub position_meta: HashMap<String, PositionMeta>,
    /// Last YES mid each held market was marked at (a closed market's book
    /// goes one-sided; its position keeps the last mark until settled).
    pub marks: HashMap<String, f64>,
    pub pnl: PnlBook,
    pub settlements: VecDeque<Value>,
    pub markouts: VecDeque<Value>,
    pub actions: VecDeque<Value>,
    pub series: Series,
    pub risk: RiskGuard,
    pub onchain_cancel_pending: bool,
    pub deadman_armed: bool,
    pub last_heartbeat_ms: i64,
}

impl AdapterState {
    pub fn new(venue: &str, mode: VenueMode, config_fingerprint: String, risk: RiskGuard) -> Self {
        let now = now_ms();
        Self {
            venue: venue.into(),
            mode,
            started_ms: now,
            config_fingerprint,
            ready: false,
            reconciling: true,
            degraded: Vec::new(),
            latched: Vec::new(),
            clock_offset_ms: None,
            feeds: HashMap::new(),
            books: HashMap::new(),
            kalshi_fairs: HashMap::new(),
            matches: HashMap::new(),
            orders: VecDeque::new(),
            fills: VecDeque::new(),
            inventory: HashMap::new(),
            positions: HashMap::new(),
            position_meta: HashMap::new(),
            marks: HashMap::new(),
            pnl: PnlBook::default(),
            settlements: VecDeque::new(),
            markouts: VecDeque::new(),
            actions: VecDeque::new(),
            series: Series::new(3600),
            risk,
            onchain_cancel_pending: false,
            deadman_armed: false,
            last_heartbeat_ms: now,
        }
    }

    /// Book a buy fill into cost basis + signed inventory, and append the fill.
    pub fn book_fill(&mut self, fill: &FillRecord) {
        let yes = is_yes_side(&fill.side);
        self.positions.entry(fill.market.clone()).or_default().apply_buy(
            &fill.side,
            fill.price,
            fill.quantity,
            fill.fee,
        );
        *self.inventory.entry(fill.market.clone()).or_default() +=
            if yes { fill.quantity } else { -fill.quantity };
        if self.inventory.get(&fill.market).is_some_and(|q| q.abs() <= 1e-9) {
            self.inventory.remove(&fill.market);
        }
        if self.positions.get(&fill.market).is_some_and(Position::is_flat) {
            self.positions.remove(&fill.market);
        }
        self.pnl.fees_total += fill.fee;
        self.push_fill(fill.clone());
        self.recount();
        self.persist_open();
    }

    /// Copy open positions into the persisted P&L book.
    fn persist_open(&mut self) {
        self.pnl.open = self.positions.clone();
        self.pnl.open_meta = self.position_meta.clone();
        self.pnl.save();
    }

    /// After a restart: positions held before it, from the P&L book.
    pub fn restore_open(&mut self) {
        self.positions = self.pnl.open.clone();
        self.position_meta = self.pnl.open_meta.clone();
        self.inventory = self.positions.iter().filter(|(_, p)| !p.is_flat()).map(|(k, p)| (k.clone(), p.net())).collect();
        self.recount();
    }

    /// Work the risk counters out from the orders and positions themselves,
    /// as the venue would see them: resting orders, their notional plus the
    /// cost of shares held (live, both are capital at risk), and markets with
    /// either. Incremental counters drifted (528 "active markets" of 4).
    pub fn recount(&mut self) {
        let open = |o: &&OrderRecord| o.status == "submitted" || o.status == "shadow_open";
        let resting: Vec<&OrderRecord> = self.orders.iter().filter(open).collect();
        let held: f64 = self.positions.values().filter(|p| !p.is_flat()).map(|p| p.yes_cost + p.no_cost).sum();
        let mut markets: std::collections::HashSet<&str> = resting.iter().map(|o| o.market.as_str()).collect();
        markets.extend(self.positions.iter().filter(|(_, p)| !p.is_flat()).map(|(k, _)| k.as_str()));
        self.risk.state.open_orders = resting.len();
        self.risk.state.open_usd = resting.iter().map(|o| o.price * o.quantity).sum::<f64>() + held;
        self.risk.state.active_markets = markets.len();
    }

    /// Positions whose market decided at least `grace_ms` ago: (market, meta).
    pub fn due_settlement(&self, now: i64, grace_ms: i64) -> Vec<(String, PositionMeta)> {
        self.positions
            .iter()
            .filter(|(_, p)| !p.is_flat())
            .filter_map(|(k, _)| {
                let meta = self.position_meta.get(k)?;
                meta.close_ms.is_some_and(|c| now - c >= grace_ms).then(|| (k.clone(), meta.clone()))
            })
            .collect()
    }

    /// Settle a held market at its outcome: realised P&L, the P&L book, and a
    /// settlement record (what was held, what it paid, where the result came from).
    pub fn settle_position(&mut self, market: &str, yes_won: bool, source: &str) -> Option<Value> {
        let pos = self.positions.get(market).cloned()?;
        let meta = self.position_meta.get(market).cloned().unwrap_or_default();
        let now = now_ms();
        let realised = self.realise_resolution(market, f64::from(u8::from(yes_won)), f64::from(u8::from(!yes_won)));
        self.pnl.realise(realised, now);
        self.position_meta.remove(market);
        self.marks.remove(market);
        let payout = if yes_won { pos.yes_qty } else { pos.no_qty };
        let record = json!({
            "market": market, "label": meta.label, "kalshi_ticker": meta.kalshi_ticker,
            "outcome": if yes_won { "YES" } else { "NO" }, "source": source,
            "yes_qty": pos.yes_qty, "yes_cost": pos.yes_cost, "no_qty": pos.no_qty, "no_cost": pos.no_cost,
            "cost": pos.yes_cost + pos.no_cost, "payout": payout, "realised": realised,
            "opened_ms": meta.opened_ms, "settled_ms": now,
        });
        bounded_push(&mut self.settlements, record.clone(), 500);
        self.recount();
        self.persist_open();
        Some(record)
    }

    /// Open positions with their marks, for the P&L view.
    pub fn open_positions(&self) -> Vec<Value> {
        self.positions
            .iter()
            .filter(|(_, p)| !p.is_flat())
            .map(|(k, p)| {
                let meta = self.position_meta.get(k).cloned().unwrap_or_default();
                let mark = self.marks.get(k).copied();
                json!({
                    "market": k, "label": meta.label, "kalshi_ticker": meta.kalshi_ticker, "close_ms": meta.close_ms,
                    "yes_qty": p.yes_qty, "yes_avg": (p.yes_qty > 1e-9).then(|| p.yes_cost / p.yes_qty),
                    "no_qty": p.no_qty, "no_avg": (p.no_qty > 1e-9).then(|| p.no_cost / p.no_qty),
                    "cost": p.yes_cost + p.no_cost, "mark": mark, "unrealised": mark.map(|m| p.marked(m)),
                })
            })
            .collect()
    }

    /// Refresh marked P&L from YES mids (`market → yes mid`). May latch daily loss.
    pub fn mark_to_market(&mut self, yes_mids: &HashMap<String, f64>) {
        for (market, mid) in yes_mids {
            if mid.is_finite() && self.positions.contains_key(market) {
                self.marks.insert(market.clone(), *mid);
            }
        }
        let mut marked = 0.0;
        for (market, pos) in &self.positions {
            if pos.is_flat() {
                continue;
            }
            if let Some(&mid) = self.marks.get(market) {
                marked += pos.marked(mid);
            }
        }
        self.risk.state.marked_pnl = marked;
        self.pnl.sample(now_ms(), marked);
        let _ = self.risk.check_latches();
    }

    /// Realise resolution for a market (YES/NO payoffs in [0, 1]) and clear it.
    pub fn realise_resolution(&mut self, market: &str, yes_payoff: f64, no_payoff: f64) -> f64 {
        let Some(mut pos) = self.positions.remove(market) else {
            self.inventory.remove(market);
            return 0.0;
        };
        let realised = pos.realise_resolution(yes_payoff, no_payoff);
        self.inventory.remove(market);
        self.risk.state.realised_pnl += realised;
        self.risk.state.active_markets =
            self.positions.values().filter(|p| !p.is_flat()).count();
        let _ = self.risk.check_latches();
        self.risk.save();
        realised
    }

    /// Realise a YES/NO merge of `amount` shares at $1.
    pub fn realise_merge(&mut self, market: &str, amount: f64) -> f64 {
        let Some(pos) = self.positions.get_mut(market) else { return 0.0 };
        let realised = pos.realise_merge(amount);
        self.pnl.realise(realised, now_ms());
        *self.inventory.entry(market.into()).or_default() = pos.net();
        if pos.is_flat() {
            self.positions.remove(market);
            self.inventory.remove(market);
        } else if self.inventory.get(market).is_some_and(|q| q.abs() <= 1e-9) {
            self.inventory.remove(market);
        }
        self.risk.state.realised_pnl += realised;
        self.risk.state.active_markets =
            self.positions.values().filter(|p| !p.is_flat()).count();
        let _ = self.risk.check_latches();
        self.risk.save();
        realised
    }

    pub fn latch(&mut self, reason: impl Into<String>) {
        let reason = reason.into();
        if !self.latched.contains(&reason) {
            self.latched.push(reason.clone());
        }
        self.ready = false;
        self.risk.halt(reason);
    }

    pub fn acknowledge(&mut self) {
        self.latched.clear();
        self.risk.acknowledge();
        self.ready = !self.reconciling && self.degraded.is_empty();
    }

    pub fn push_action(&mut self, value: Value) {
        bounded_push(&mut self.actions, value, 200);
    }

    pub fn push_order(&mut self, value: OrderRecord) {
        bounded_push(&mut self.orders, value, 500);
    }

    pub fn push_fill(&mut self, value: FillRecord) {
        bounded_push(&mut self.fills, value, 500);
    }

    pub fn push_markout(&mut self, value: Value) {
        bounded_push(&mut self.markouts, value, 500);
    }

    /// The account view: bankroll, equity, realised (today, total, by day),
    /// unrealised, fees, settled positions and win rate, the equity curve.
    pub fn pnl_status(&self) -> Value {
        let p = &self.pnl;
        let marked = self.risk.state.marked_pnl;
        let today = Utc::now().format("%Y-%m-%d").to_string();
        let decided = p.wins + p.losses;
        json!({
            "capital": p.capital,
            "equity": p.capital + p.realised_total + marked,
            "realised_total": p.realised_total,
            "realised_today": p.realised_by_day.get(&today).copied().unwrap_or(0.0),
            "unrealised": marked,
            "fees_total": p.fees_total,
            "return_pct": if p.capital > 0.0 { Some((p.realised_total + marked) / p.capital * 100.0) } else { None },
            "settled": p.settled,
            "wins": p.wins,
            "losses": p.losses,
            "win_rate": (decided > 0).then(|| p.wins as f64 / decided as f64),
            "avg_per_settled": (p.settled > 0).then(|| p.realised_total / p.settled as f64),
            "by_day": p.realised_by_day,
            "series": p.series.iter().map(|(t, r, m)| json!([t, r, m])).collect::<Vec<_>>(),
        })
    }

    pub fn status(&self) -> Value {
        let now = now_ms();
        json!({
            "venue": self.venue,
            "mode": self.mode,
            "ready": self.ready && self.latched.is_empty() && !self.reconciling,
            "reconciling": self.reconciling,
            "uptime_s": (now - self.started_ms).max(0) / 1000,
            "build": option_env!("GIT_COMMIT").unwrap_or("dev"),
            "config": self.config_fingerprint,
            "clock_offset_ms": self.clock_offset_ms,
            "feeds": self.feeds,
            "books": self.books,
            "kalshi_fairs": self.kalshi_fairs,
            "matches": self.matches,
            "inventory": self.inventory,
            "positions": self.positions,
            "open_positions": self.open_positions(),
            "pnl": self.pnl_status(),
            "risk": self.risk.status(),
            "latched": self.latched,
            "degraded": self.degraded,
            "onchain_cancel_pending": self.onchain_cancel_pending,
            "deadman_armed": self.deadman_armed,
            "last_heartbeat_ms": self.last_heartbeat_ms,
            "recent_actions": self.actions,
            "recent_orders": self.orders,
            "recent_fills": self.fills,
            "settlements": self.settlements,
            "markouts": self.markouts,
        })
    }
}

fn bounded_push<T>(values: &mut VecDeque<T>, value: T, max: usize) {
    while values.len() >= max {
        values.pop_front();
    }
    values.push_back(value);
}

pub struct Journal {
    root: PathBuf,
    current_day: String,
    file: File,
}

impl Journal {
    pub fn open(root: impl Into<PathBuf>) -> anyhow::Result<Self> {
        let root = root.into();
        std::fs::create_dir_all(&root)?;
        let current_day = Utc::now().format("%Y-%m-%d").to_string();
        let file = open_day(&root, &current_day)?;
        Ok(Self { root, current_day, file })
    }

    pub fn record(&mut self, kind: &str, mut value: Value) -> anyhow::Result<()> {
        let day = Utc::now().format("%Y-%m-%d").to_string();
        if day != self.current_day {
            self.file = open_day(&self.root, &day)?;
            self.current_day = day;
        }
        let object =
            value.as_object_mut().ok_or_else(|| anyhow::anyhow!("journal value must be an object"))?;
        object.insert("type".into(), kind.into());
        object.insert("t_ms".into(), now_ms().into());
        redact_secrets(&mut value);
        serde_json::to_writer(&mut self.file, &value)?;
        self.file.write_all(b"\n")?;
        self.file.flush()?;
        Ok(())
    }
}

fn redact_secrets(value: &mut Value) {
    match value {
        Value::Object(object) => {
            for (key, value) in object {
                let key = key.to_ascii_lowercase().replace(['-', '_'], "");
                if matches!(
                    key.as_str(),
                    "authorization"
                        | "xapikey"
                        | "apikey"
                        | "jwt"
                        | "accesstoken"
                        | "idtoken"
                        | "privatekey"
                        | "signature"
                        | "signedpayload"
                ) {
                    *value = Value::String("[redacted]".into());
                } else {
                    redact_secrets(value);
                }
            }
        }
        Value::Array(values) => values.iter_mut().for_each(redact_secrets),
        _ => {}
    }
}

fn open_day(root: &Path, day: &str) -> anyhow::Result<File> {
    Ok(OpenOptions::new().create(true).append(true).open(root.join(format!("{day}.jsonl")))?)
}

pub fn now_ms() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis() as i64)
        .unwrap_or(0)
}

/// Post-fill markouts at 5, 30 and 60 seconds against the venue mid.
/// Callers pass the mid in the fill's own side terms (both lanes only buy),
/// so the markout is always `mid(+t) - fill_px`.
pub struct MarkoutTracker {
    pending: VecDeque<MarkoutPending>,
}

struct MarkoutPending {
    market: String,
    side: String,
    fill_px: f64,
    fill_ms: i64,
    done: [bool; 3],
}

impl MarkoutTracker {
    pub fn new() -> Self {
        Self { pending: VecDeque::new() }
    }

    pub fn watch(&mut self, market: &str, side: &str, fill_px: f64, fill_ms: i64) {
        if self.pending.len() >= 200 {
            self.pending.pop_front();
        }
        self.pending.push_back(MarkoutPending {
            market: market.into(),
            side: side.into(),
            fill_px,
            fill_ms,
            done: [false; 3],
        });
    }

    /// Returns finished markout records; drops entries older than 5 minutes.
    /// `mids` is keyed by `(market, side)` so YES and NO fills on one market
    /// each compare against their own side's mid.
    pub fn poll(&mut self, now_ms: i64, mids: &HashMap<(String, String), f64>) -> Vec<Value> {
        const HORIZONS: [i64; 3] = [5_000, 30_000, 60_000];
        let mut out = Vec::new();
        self.pending.retain(|p| now_ms - p.fill_ms < 300_000);
        for pending in self.pending.iter_mut() {
            let key = (pending.market.clone(), pending.side.clone());
            let Some(mid) = mids.get(&key).copied() else { continue };
            for (i, horizon) in HORIZONS.iter().enumerate() {
                if !pending.done[i] && now_ms - pending.fill_ms >= *horizon {
                    pending.done[i] = true;
                    out.push(json!({
                        "market": pending.market,
                        "side": pending.side,
                        "fill_px": pending.fill_px,
                        "horizon_s": horizon / 1000,
                        "markout": mid - pending.fill_px,
                    }));
                }
            }
        }
        self.pending.retain(|p| p.done.iter().any(|d| !d));
        out
    }
}

impl Default for MarkoutTracker {
    fn default() -> Self {
        Self::new()
    }
}

/// Preserve only a short prefix and never include request bodies, signed
/// payloads, credentials, signatures or authorization headers.
pub fn public_error(error: &dyn std::fmt::Display) -> String {
    let raw = error.to_string();
    let lower = raw.to_ascii_lowercase();
    if ["private", "authorization", "jwt", "signature", "api-key", "x-api-key", "signed payload"]
        .iter()
        .any(|needle| lower.contains(needle))
    {
        "redacted authentication error".into()
    } else {
        raw.chars().take(240).collect()
    }
}

/// Read chrony's latest system clock offset. A missing chronyc binary is an
/// error in production; the systemd units also gate startup with waitsync.
pub fn chrony_offset_ms() -> anyhow::Result<i64> {
    let output = std::process::Command::new("chronyc").args(["tracking", "-n"]).output()?;
    anyhow::ensure!(output.status.success(), "chronyc tracking failed");
    let text = String::from_utf8(output.stdout)?;
    let line = text
        .lines()
        .find(|line| line.starts_with("Last offset"))
        .ok_or_else(|| anyhow::anyhow!("chronyc output has no Last offset"))?;
    let value = line
        .split(':')
        .nth(1)
        .and_then(|x| x.split_whitespace().next())
        .ok_or_else(|| anyhow::anyhow!("chronyc Last offset is malformed"))?
        .parse::<f64>()?;
    Ok((value.abs() * 1000.0).round() as i64)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{RiskLimits, VenueMode};

    fn guard(path: &str) -> RiskGuard {
        RiskGuard::open(
            RiskLimits {
                order_usd: 10.0,
                max_open_usd: 20.0,
                daily_loss_usd: 5.0,
                max_markets: 2,
                max_orders: 4,
                max_rejections: 3,
            },
            std::env::temp_dir().join(format!("pk-venue-svc-{path}-{}", std::process::id())),
        )
        .unwrap()
    }

    #[test]
    fn latch_halts_and_acknowledge_restores_readiness() {
        let mut state = AdapterState::new("predict", VenueMode::Shadow, "cfg".into(), guard("latch"));
        state.reconciling = false;
        state.ready = true;
        state.latch("boom");
        assert!(!state.ready);
        assert!(!state.status()["ready"].as_bool().unwrap());
        assert!(state.risk.check_order(1.0, false).is_err());
        state.acknowledge();
        assert!(state.latched.is_empty());
        assert!(state.ready);
    }

    #[test]
    fn journal_redacts_secrets_and_stamps_every_record() {
        let dir = std::env::temp_dir().join(format!("pk-venue-journal-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        let mut journal = Journal::open(&dir).unwrap();
        journal
            .record(
                "order_submitted",
                json!({
                    "order": {"id": "1", "signature": "0xdeadbeef", "signedPayload": {"x": 1}},
                    "jwt": "eyJhbGciOiJIUzI1NiJ9",
                    "privateKey": "0xcafe",
                    "authorization": "Bearer hunter2",
                    "price": 0.5,
                }),
            )
            .unwrap();
        let day = Utc::now().format("%Y-%m-%d").to_string();
        let line = std::fs::read_to_string(dir.join(format!("{day}.jsonl"))).unwrap();
        assert!(!line.contains("deadbeef"), "{line}");
        assert!(!line.contains("eyJhbGciOiJIUzI1NiJ9"), "{line}");
        assert!(!line.contains("cafe"), "{line}");
        assert!(!line.contains("hunter2"), "{line}");
        assert!(line.contains("[redacted]"), "{line}");
        assert!(line.contains(r#""type":"order_submitted""#), "{line}");
        assert!(line.contains(r#""price":0.5"#), "{line}");
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn public_error_redacts_auth_material_and_truncates() {
        let secret = anyhow::anyhow!("request failed with signature 0xdeadbeef attached");
        assert_eq!(public_error(&secret), "redacted authentication error");
        let long = anyhow::anyhow!("{}", "x".repeat(500));
        assert_eq!(public_error(&long).len(), 240);
        let plain = anyhow::anyhow!("Predict HTTP 429: rate limited");
        assert_eq!(public_error(&plain), "Predict HTTP 429: rate limited");
    }

    #[test]
    fn markouts_fire_at_each_horizon_then_release() {
        let mut tracker = MarkoutTracker::new();
        tracker.watch("7", "YES", 0.40, 1_000_000);
        let mids = HashMap::from([(("7".into(), "YES".into()), 0.45)]);
        assert!(tracker.poll(1_000_000 + 4_999, &mids).is_empty());
        let five = tracker.poll(1_000_000 + 5_000, &mids);
        assert_eq!(five.len(), 1);
        assert_eq!(five[0]["horizon_s"], 5);
        assert!((five[0]["markout"].as_f64().unwrap() - 0.05).abs() < 1e-9);
        assert_eq!(tracker.poll(1_000_000 + 30_000, &mids).len(), 1);
        assert_eq!(tracker.poll(1_000_000 + 60_000, &mids).len(), 1);
        assert!(tracker.poll(1_000_000 + 61_000, &mids).is_empty());
    }

    #[test]
    fn position_cost_basis_marks_and_realises() {
        let mut p = Position::default();
        p.apply_buy("YES", 0.40, 10.0, 0.0);
        p.apply_buy("NO", 0.55, 10.0, 0.0);
        // Locked pair: 10 × (1 − 0.40 − 0.55) = +0.50 at any mid.
        assert!((p.marked(0.50) - 0.50).abs() < 1e-9);
        assert!((p.realise_merge(10.0) - 0.50).abs() < 1e-9);
        assert!(p.is_flat());

        p.apply_buy("yes", 0.30, 5.0, 0.1);
        assert!((p.marked(0.50) - (5.0 * 0.50 - 1.6)).abs() < 1e-9);
        let r = p.realise_resolution(1.0, 0.0);
        assert!((r - (5.0 - 1.6)).abs() < 1e-9);
        assert!(p.is_flat());
        assert!(is_yes_side("BUY") && is_yes_side("b") && !is_yes_side("NO"));
    }

    #[test]
    fn recount_settle_and_pnl_book_are_consistent() {
        let dir = std::env::temp_dir().join(format!("pk-venue-book-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let limits = crate::RiskLimits { order_usd: 10.0, max_open_usd: 50.0, daily_loss_usd: 25.0, max_markets: 4, max_orders: 16, max_rejections: 5 };
        let risk = RiskGuard::open(limits, dir.join("risk.json")).unwrap();
        let mut state = AdapterState::new("predict", VenueMode::Shadow, "cfg".into(), risk);
        state.pnl = PnlBook::open(dir.join("pnl.json"), 100.0);
        // Requotes never inflate the counts: two resting orders on one market.
        for i in 0..50 {
            state.push_order(OrderRecord { id: format!("o{i}"), client_id: String::new(), market: "7".into(), lane: "maker".into(), side: "YES".into(), price: 0.4, quantity: 1.0, status: if i >= 48 { "shadow_open".into() } else { "removed_off_book".into() }, created_ms: 0, updated_ms: 0, simulated: true });
            state.recount();
        }
        assert_eq!((state.risk.state.open_orders, state.risk.state.active_markets), (2, 1));
        // A held position counts toward exposure and markets.
        state.position_meta.insert("9".into(), PositionMeta { close_ms: Some(1_000), kalshi_ticker: Some("KX".into()), label: "btc".into(), opened_ms: 0 });
        state.book_fill(&FillRecord { id: "f".into(), order_id: "x".into(), market: "9".into(), side: "YES".into(), price: 0.30, quantity: 10.0, fee: 0.05, time_ms: 0, simulated: true });
        assert_eq!(state.risk.state.active_markets, 2);
        assert!((state.risk.state.open_usd - (0.8 + 3.05)).abs() < 1e-9);
        // A closed market keeps its last mark.
        state.mark_to_market(&HashMap::from([("9".into(), 0.60)]));
        state.mark_to_market(&HashMap::new());
        assert!((state.risk.state.marked_pnl - (6.0 - 3.05)).abs() < 1e-9);
        assert_eq!(state.due_settlement(70_000, 60_000).len(), 1);
        let rec = state.settle_position("9", true, "predict").unwrap();
        assert!((rec["realised"].as_f64().unwrap() - (10.0 - 3.05)).abs() < 1e-9);
        assert_eq!((state.pnl.settled, state.pnl.wins), (1, 1));
        assert!((state.pnl.fees_total - 0.05).abs() < 1e-9);
        assert_eq!(state.risk.state.active_markets, 1);
        // The book persists, with what is still held.
        state.book_fill(&FillRecord { id: "g".into(), order_id: "y".into(), market: "11".into(), side: "NO".into(), price: 0.45, quantity: 4.0, fee: 0.0, time_ms: 0, simulated: true });
        let again = PnlBook::open(dir.join("pnl.json"), 100.0);
        assert!((again.realised_total - 6.95).abs() < 1e-9);
        let risk = RiskGuard::open(limits, dir.join("risk2.json")).unwrap();
        let mut restarted = AdapterState::new("predict", VenueMode::Shadow, "cfg".into(), risk);
        restarted.pnl = again;
        restarted.restore_open();
        assert_eq!(restarted.inventory.get("11"), Some(&-4.0));
        assert!((restarted.risk.state.open_usd - 1.8).abs() < 1e-9);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn adapter_books_fill_marks_and_realises_into_risk() {
        let dir = std::env::temp_dir().join(format!("pk-venue-pnl-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let risk = RiskGuard::open(
            crate::RiskLimits {
                order_usd: 10.0,
                max_open_usd: 50.0,
                daily_loss_usd: 25.0,
                max_markets: 4,
                max_orders: 16,
                max_rejections: 5,
            },
            dir.join("risk.json"),
        )
        .unwrap();
        let mut state = AdapterState::new("predict", VenueMode::Shadow, "cfg".into(), risk);
        let fill = FillRecord {
            id: "f1".into(),
            order_id: "o1".into(),
            market: "7".into(),
            side: "YES".into(),
            price: 0.40,
            quantity: 5.0,
            fee: 0.0,
            time_ms: 1,
            simulated: true,
        };
        state.book_fill(&fill);
        assert_eq!(state.inventory.get("7"), Some(&5.0));
        assert_eq!(state.fills.len(), 1);
        state.mark_to_market(&HashMap::from([("7".into(), 0.50)]));
        assert!((state.risk.state.marked_pnl - 0.50).abs() < 1e-9);
        let realised = state.realise_resolution("7", 1.0, 0.0);
        assert!((realised - 3.0).abs() < 1e-9);
        assert!((state.risk.state.realised_pnl - 3.0).abs() < 1e-9);
        assert!(state.positions.is_empty());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn atomic_write_creates_parents_and_lands_whole() {
        let dir = std::env::temp_dir().join(format!("pk-venue-atomic-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        let path = dir.join("nested").join("state.json");
        crate::atomic_write(&path, b"{\"a\":1}").unwrap();
        assert_eq!(std::fs::read(&path).unwrap(), b"{\"a\":1}");
        assert!(std::fs::read_dir(dir.join("nested")).unwrap().count() == 1);
        let _ = std::fs::remove_dir_all(dir);
    }
}
