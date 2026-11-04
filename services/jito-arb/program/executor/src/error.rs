use solana_program::program_error::ProgramError;

/// Custom program errors. `MinOutNotMet` is the guard: the runtime reverts every instruction in the
/// transaction, the flash borrow included, so a losing route can never land.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[repr(u32)]
pub enum ExecutorError {
    InvalidInstruction = 0x1770, // 6000
    Unauthorized = 0x1771,
    MinOutNotMet = 0x1772,
    UnsupportedDex = 0x1773,
    AccountRange = 0x1774,
    WrongDexProgram = 0x1775,
    BadTokenAccount = 0x1776,
    Overflow = 0x1777,
}

impl From<ExecutorError> for ProgramError {
    fn from(e: ExecutorError) -> Self {
        ProgramError::Custom(e as u32)
    }
}
