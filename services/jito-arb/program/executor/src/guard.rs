//! The end-of-route profit check (spec §8 steps 2, 4, 5).

/// Output of the route in base units, from the base ATA balance before and after.
/// `pre` already includes the flash-borrowed `amount_in`, so the route's result is
/// `post - (pre - amount_in)`. None on an impossible balance (underflow).
pub fn route_out(pre: u64, post: u64, amount_in: u64) -> Option<u64> {
    let start = pre.checked_sub(amount_in)?;
    post.checked_sub(start)
}

/// The guard: true when the route returned at least `min_out`.
pub fn meets_min_out(pre: u64, post: u64, amount_in: u64, min_out: u64) -> bool {
    matches!(route_out(pre, post, amount_in), Some(out) if out >= min_out)
}
