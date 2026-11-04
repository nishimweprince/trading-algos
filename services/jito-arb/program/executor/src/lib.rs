//! jito-arb executor (spec §8): a small, stateless native program. It executes the swap legs of one
//! cycle by CPI, measures the real balance changes, and fails the whole transaction unless the final
//! output meets `min_out`. Not compiled on the development laptop (no Rust toolchain there): build and
//! test it on the VM (`cargo build-sbf`, `cargo test`, tests/validator.sh).
pub mod args;
pub mod error;
pub mod guard;
pub mod processor;

#[cfg(not(feature = "no-entrypoint"))]
solana_program::entrypoint!(processor::process_instruction);
