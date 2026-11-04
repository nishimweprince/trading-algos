//! `execute_route` instruction data, packed little-endian (spec §8). The C++ encoder
//! (`src/tx/builders.cpp` encode_execute_route) and this parser share the fixture
//! `tests/fixtures/execute_route.json`.
//!
//!   u8 tag (0) | u64 amount_in | u64 min_out | u8 num_legs | 3 x LegSpec { u8 dex, u8 a_to_b, u8 acct_start, u8 acct_len }

pub const EXECUTE_ROUTE_TAG: u8 = 0;
pub const DATA_LEN: usize = 1 + 8 + 8 + 1 + 3 * 4;
pub const MAX_LEGS: usize = 3;

/// DEX enum, as `DexKind` in src/state/pool_state.hpp.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[repr(u8)]
pub enum Dex {
    RaydiumV4 = 0,
    Whirlpool = 1,
    Cpmm = 2,
    Dlmm = 3,
    Clmm = 4,
    PumpSwap = 5,
}

impl Dex {
    pub fn from_u8(v: u8) -> Option<Dex> {
        Some(match v {
            0 => Dex::RaydiumV4,
            1 => Dex::Whirlpool,
            2 => Dex::Cpmm,
            3 => Dex::Dlmm,
            4 => Dex::Clmm,
            5 => Dex::PumpSwap,
            _ => return None,
        })
    }
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct LegSpec {
    pub dex: u8,
    pub a_to_b: u8,
    pub acct_start: u8,
    pub acct_len: u8,
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct ExecuteRouteArgs {
    pub amount_in: u64,
    pub min_out: u64,
    pub num_legs: u8,
    pub legs: [LegSpec; MAX_LEGS],
}

#[derive(Debug, PartialEq, Eq)]
pub enum ParseError {
    Length,
    Tag,
    LegCount,
}

impl ExecuteRouteArgs {
    pub fn parse(data: &[u8]) -> Result<Self, ParseError> {
        if data.len() != DATA_LEN {
            return Err(ParseError::Length);
        }
        if data[0] != EXECUTE_ROUTE_TAG {
            return Err(ParseError::Tag);
        }
        let amount_in = u64::from_le_bytes(data[1..9].try_into().unwrap());
        let min_out = u64::from_le_bytes(data[9..17].try_into().unwrap());
        let num_legs = data[17];
        if !(2..=MAX_LEGS as u8).contains(&num_legs) {
            return Err(ParseError::LegCount);
        }
        let mut legs = [LegSpec::default(); MAX_LEGS];
        for (i, leg) in legs.iter_mut().enumerate() {
            let o = 18 + 4 * i;
            *leg = LegSpec { dex: data[o], a_to_b: data[o + 1], acct_start: data[o + 2], acct_len: data[o + 3] };
        }
        Ok(ExecuteRouteArgs { amount_in, min_out, num_legs, legs })
    }
}
