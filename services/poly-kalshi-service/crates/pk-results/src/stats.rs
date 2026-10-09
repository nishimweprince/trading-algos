//! Python's `statistics` as the results use it, and record accessors.

use serde_json::Value;

/// `statistics.fmean`: an accurately rounded sum over n.
pub fn fmean(v: &[f64]) -> Option<f64> {
    if v.is_empty() {
        return None;
    }
    Some(fsum(v) / v.len() as f64)
}

/// Neumaier-compensated summation, close to Python's `math.fsum`.
pub fn fsum(v: &[f64]) -> f64 {
    let (mut sum, mut c) = (0.0f64, 0.0f64);
    for &x in v {
        let t = sum + x;
        if sum.abs() >= x.abs() {
            c += (sum - t) + x;
        } else {
            c += (x - t) + sum;
        }
        sum = t;
    }
    sum + c
}

/// `statistics.median`: the middle value, or the mean of the two middle values.
pub fn median(v: &[f64]) -> Option<f64> {
    if v.is_empty() {
        return None;
    }
    let mut s = v.to_vec();
    s.sort_by(f64::total_cmp);
    let n = s.len();
    Some(if n % 2 == 1 { s[n / 2] } else { (s[n / 2 - 1] + s[n / 2]) / 2.0 })
}

/// `statistics.stdev` (sample).
pub fn stdev(v: &[f64]) -> Option<f64> {
    if v.len() < 2 {
        return None;
    }
    let m = fmean(v)?;
    let ss: Vec<f64> = v.iter().map(|x| (x - m) * (x - m)).collect();
    Some((fsum(&ss) / (v.len() - 1) as f64).sqrt())
}

pub fn f(r: &Value, k: &str) -> Option<f64> {
    r.get(k).and_then(Value::as_f64)
}

pub fn s<'a>(r: &'a Value, k: &str) -> Option<&'a str> {
    r.get(k).and_then(Value::as_str)
}

pub fn i(r: &Value, k: &str) -> Option<i64> {
    r.get(k).and_then(|v| v.as_i64().or_else(|| v.as_f64().map(|f| f as i64)))
}

/// Python truthiness of a field.
pub fn truthy(r: &Value, k: &str) -> bool {
    match r.get(k) {
        None | Some(Value::Null) => false,
        Some(Value::Bool(b)) => *b,
        Some(Value::Number(n)) => n.as_f64() != Some(0.0),
        Some(Value::String(s)) => !s.is_empty(),
        Some(Value::Array(a)) => !a.is_empty(),
        Some(Value::Object(o)) => !o.is_empty(),
    }
}

pub type Key = (Option<String>, Option<i64>);

pub fn key(r: &Value) -> Key {
    (s(r, "asset").map(str::to_owned), i(r, "open_unix"))
}

pub fn utc_day_s(unix_s: i64) -> String {
    chrono::DateTime::<chrono::Utc>::from_timestamp(unix_s, 0)
        .map(|t| t.format("%Y-%m-%d").to_string())
        .unwrap_or_default()
}

pub fn utc_day_ns(t_ns: i64) -> String {
    chrono::DateTime::<chrono::Utc>::from_timestamp_nanos(t_ns).format("%Y-%m-%d").to_string()
}

pub fn opt(v: Option<f64>) -> Value {
    v.map(Value::from).unwrap_or(Value::Null)
}
