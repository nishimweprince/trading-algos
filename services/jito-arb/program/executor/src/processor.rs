//! execute_route (spec §8): owner check, per-leg CPI with measured balances, end-of-route min_out.
//!
//! Accounts: [0] signer (the hot wallet, must equal OWNER), [1] base-token ATA, [2 .. 2+n-1] one ATA per
//! intermediate token, then the legs' accounts. Leg i's slice is remaining[acct_start .. acct_start+acct_len]
//! and starts with the DEX program, followed by the swap's accounts in that program's order (the same lists
//! the C++ builder proved against mainnet with simulateTransaction, src/tx/dex_ix.cpp).
use solana_program::{
    account_info::AccountInfo,
    entrypoint::ProgramResult,
    instruction::{AccountMeta, Instruction},
    program::invoke,
    pubkey,
    pubkey::Pubkey,
};

use crate::args::{Dex, ExecuteRouteArgs, LegSpec};
use crate::error::ExecutorError;
use crate::guard;

/// Only this key may route through the program. Placeholder until deployment: set to the hot wallet's
/// pubkey before `cargo build-sbf` (RUNBOOK-VM.md). The upgrade authority is a separate key.
pub const OWNER: Pubkey = pubkey!("11111111111111111111111111111111");

pub const RAYDIUM_V4: Pubkey = pubkey!("675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8");
pub const WHIRLPOOL: Pubkey = pubkey!("whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc");
pub const RAYDIUM_CPMM: Pubkey = pubkey!("CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C");
pub const RAYDIUM_CLMM: Pubkey = pubkey!("CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK");
pub const PUMPSWAP: Pubkey = pubkey!("pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA");
const TOKEN: Pubkey = pubkey!("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA");
const TOKEN_2022: Pubkey = pubkey!("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb");

const RAYDIUM_SWAP_BASE_IN_V2: u8 = 16;
const WHIRLPOOL_SWAP_DISC: [u8; 8] = [248, 198, 158, 145, 225, 117, 135, 200];
const CPMM_SWAP_BASE_INPUT_DISC: [u8; 8] = [143, 190, 90, 218, 196, 30, 51, 222];
const CLMM_SWAP_V2_DISC: [u8; 8] = [43, 4, 237, 11, 26, 201, 30, 98];
const PUMP_SELL_V2_DISC: [u8; 8] = [93, 246, 130, 60, 231, 233, 64, 178];
const PUMP_BUY_EXACT_QUOTE_IN_V2_DISC: [u8; 8] = [194, 171, 28, 70, 104, 77, 91, 47];

#[cfg(feature = "logs")]
macro_rules! log { ($($t:tt)*) => { solana_program::msg!($($t)*) } }
#[cfg(not(feature = "logs"))]
macro_rules! log { ($($t:tt)*) => {} }

/// SPL Token / Token-2022 account amount (bytes 64..72), with an owner check on the account.
fn token_amount(a: &AccountInfo) -> Result<u64, ExecutorError> {
    if *a.owner != TOKEN && *a.owner != TOKEN_2022 {
        return Err(ExecutorError::BadTokenAccount);
    }
    let d = a.try_borrow_data().map_err(|_| ExecutorError::BadTokenAccount)?;
    if d.len() < 72 {
        return Err(ExecutorError::BadTokenAccount);
    }
    Ok(u64::from_le_bytes(d[64..72].try_into().unwrap()))
}

fn swap_data(dex: Dex, a_to_b: bool, amount_in: u64) -> Result<Vec<u8>, ExecutorError> {
    let mut d = Vec::with_capacity(42);
    match dex {
        Dex::RaydiumV4 => {
            // swap_base_in_v2(amount_in, minimum_amount_out = 0): the end-of-route check protects the trade.
            d.push(RAYDIUM_SWAP_BASE_IN_V2);
            d.extend_from_slice(&amount_in.to_le_bytes());
            d.extend_from_slice(&0u64.to_le_bytes());
        }
        Dex::Whirlpool => {
            // swap(amount, other_amount_threshold = 0, sqrt_price_limit = 0 (none), amount_specified_is_input, a_to_b)
            d.extend_from_slice(&WHIRLPOOL_SWAP_DISC);
            d.extend_from_slice(&amount_in.to_le_bytes());
            d.extend_from_slice(&0u64.to_le_bytes());
            d.extend_from_slice(&0u128.to_le_bytes());
            d.push(1);
            d.push(a_to_b as u8);
        }
        Dex::Cpmm => {
            // swap_base_input(amount_in, minimum_amount_out = 0)
            d.extend_from_slice(&CPMM_SWAP_BASE_INPUT_DISC);
            d.extend_from_slice(&amount_in.to_le_bytes());
            d.extend_from_slice(&0u64.to_le_bytes());
        }
        Dex::Clmm => {
            // swap_v2(amount, other_amount_threshold = 0, sqrt_price_limit_x64 = 0 (default), is_base_input = true)
            d.extend_from_slice(&CLMM_SWAP_V2_DISC);
            d.extend_from_slice(&amount_in.to_le_bytes());
            d.extend_from_slice(&0u64.to_le_bytes());
            d.extend_from_slice(&0u128.to_le_bytes());
            d.push(1);
        }
        Dex::PumpSwap => {
            // a_to_b (base -> quote): sell_v2(base_amount_in, min_quote_amount_out = 0);
            // b_to_a: buy_exact_quote_in_v2(spendable_quote_in, min_base_amount_out = 1), whose minimum must be > 0.
            if a_to_b {
                d.extend_from_slice(&PUMP_SELL_V2_DISC);
                d.extend_from_slice(&amount_in.to_le_bytes());
                d.extend_from_slice(&0u64.to_le_bytes());
            } else {
                d.extend_from_slice(&PUMP_BUY_EXACT_QUOTE_IN_V2_DISC);
                d.extend_from_slice(&amount_in.to_le_bytes());
                d.extend_from_slice(&1u64.to_le_bytes());
            }
        }
        _ => return Err(ExecutorError::UnsupportedDex),
    }
    Ok(d)
}

fn run_leg<'a>(leg: &LegSpec, legs_accounts: &[AccountInfo<'a>], amount_in: u64) -> ProgramResult {
    let dex = Dex::from_u8(leg.dex).ok_or(ExecutorError::UnsupportedDex)?;
    let start = leg.acct_start as usize;
    let end = start.checked_add(leg.acct_len as usize).ok_or(ExecutorError::AccountRange)?;
    if leg.acct_len < 2 || end > legs_accounts.len() {
        return Err(ExecutorError::AccountRange.into());
    }
    let slice = &legs_accounts[start..end];
    let program = &slice[0];
    let expected = match dex {
        Dex::RaydiumV4 => RAYDIUM_V4,
        Dex::Whirlpool => WHIRLPOOL,
        Dex::Cpmm => RAYDIUM_CPMM,
        Dex::PumpSwap => PUMPSWAP,
        Dex::Clmm => RAYDIUM_CLMM,
        _ => return Err(ExecutorError::UnsupportedDex.into()),
    };
    if *program.key != expected {
        return Err(ExecutorError::WrongDexProgram.into());
    }
    let metas: Vec<AccountMeta> = slice[1..]
        .iter()
        .map(|a| AccountMeta { pubkey: *a.key, is_signer: a.is_signer, is_writable: a.is_writable })
        .collect();
    let ix = Instruction { program_id: expected, accounts: metas, data: swap_data(dex, leg.a_to_b != 0, amount_in)? };
    invoke(&ix, slice)
}

pub fn process_instruction(_program_id: &Pubkey, accounts: &[AccountInfo], data: &[u8]) -> ProgramResult {
    let args = ExecuteRouteArgs::parse(data).map_err(|_| ExecutorError::InvalidInstruction)?;
    let n = args.num_legs as usize;
    let fixed = 2 + (n - 1);
    if accounts.len() < fixed {
        return Err(ExecutorError::AccountRange.into());
    }
    // 1. Only the owner may route through this program.
    let signer = &accounts[0];
    if !signer.is_signer || *signer.key != OWNER {
        return Err(ExecutorError::Unauthorized.into());
    }
    let base = &accounts[1];
    let inter = &accounts[2..fixed];
    let legs_accounts = &accounts[fixed..];
    // 2. Base balance before; it already includes the borrowed amount_in.
    let pre = token_amount(base)?;
    // 3. Each leg takes the previous leg's measured output.
    let mut amount = args.amount_in;
    for i in 0..n {
        let out_acct = if i + 1 == n { base } else { &inter[i] };
        let before = token_amount(out_acct)?;
        run_leg(&args.legs[i], legs_accounts, amount)?;
        let after = token_amount(out_acct)?;
        amount = after.checked_sub(before).ok_or(ExecutorError::Overflow)?;
        log!("leg {} out {}", i, amount);
    }
    // 4-5. The guard.
    let post = token_amount(base)?;
    if !guard::meets_min_out(pre, post, args.amount_in, args.min_out) {
        log!("min_out not met: pre {} post {} in {} min_out {}", pre, post, args.amount_in, args.min_out);
        return Err(ExecutorError::MinOutNotMet.into());
    }
    Ok(())
}
