//! CP-specific executor checks against the same independent IDL goldens as C++.
use super::*;
use solana_program::program_stubs::{set_syscall_stubs, SyscallStubs};
use std::{
    str::FromStr,
    sync::{Arc, Mutex},
};

struct Capture(Arc<Mutex<Vec<Instruction>>>);
impl SyscallStubs for Capture {
    fn sol_invoke_signed(
        &self,
        ix: &Instruction,
        _: &[AccountInfo],
        _: &[&[&[u8]]],
    ) -> ProgramResult {
        self.0.lock().unwrap().push(ix.clone());
        Ok(())
    }
}

#[test]
fn cp_programs_accounts_directions_and_data_match_pinned_idls() {
    let fixture: serde_json::Value =
        serde_json::from_str(include_str!("../../../tests/golden_tx/cp_swaps.json")).unwrap();
    assert_eq!(
        RAYDIUM_CPMM.to_string(),
        fixture["programs"]["cpmm"].as_str().unwrap()
    );
    assert_eq!(
        PUMPSWAP.to_string(),
        fixture["programs"]["pumpswap"].as_str().unwrap()
    );
    let captured = Arc::new(Mutex::new(Vec::<Instruction>::new()));
    let old = set_syscall_stubs(Box::new(Capture(captured.clone())));
    for c in fixture["cases"].as_array().unwrap() {
        let cpmm = c["dex"] == "cpmm";
        let dir = c["a_to_b"].as_bool().unwrap();
        let idl: serde_json::Value = serde_json::from_str(if cpmm {
            include_str!("../../../harness/idl/raydium_cp_swap.json")
        } else {
            include_str!("../../../harness/idl/pump_amm.json")
        })
        .unwrap();
        let name = if cpmm {
            "swap_base_input"
        } else if dir {
            "sell_v2"
        } else {
            "buy_exact_quote_in_v2"
        };
        let source = idl["instructions"]
            .as_array()
            .unwrap()
            .iter()
            .find(|i| i["name"] == name)
            .unwrap();
        let mut expected: Vec<u8> = source["discriminator"]
            .as_array()
            .unwrap()
            .iter()
            .map(|x| x.as_u64().unwrap() as u8)
            .collect();
        expected.extend_from_slice(&123456789u64.to_le_bytes());
        expected.extend_from_slice(&(if !cpmm && !dir { 1u64 } else { 0u64 }).to_le_bytes());
        let dex = if cpmm { Dex::Cpmm } else { Dex::PumpSwap };
        assert_eq!(swap_data(dex, dir, 123456789).unwrap(), expected);
        let metas = c["accounts"].as_array().unwrap();
        let mut keys = vec![Pubkey::from_str(c["program"].as_str().unwrap()).unwrap()];
        keys.extend(
            metas
                .iter()
                .map(|a| Pubkey::from_str(a["key"].as_str().unwrap()).unwrap()),
        );
        let mut lamports = vec![0; keys.len()];
        let mut data = vec![Vec::<u8>::new(); keys.len()];
        let owner = solana_program::system_program::id();
        let infos: Vec<_> = keys
            .iter()
            .zip(lamports.iter_mut())
            .zip(data.iter_mut())
            .enumerate()
            .map(|(i, ((key, l), d))| {
                AccountInfo::new(
                    key,
                    i > 0 && metas[i - 1]["signer"].as_bool().unwrap(),
                    i > 0 && metas[i - 1]["writable"].as_bool().unwrap(),
                    l,
                    d,
                    &owner,
                    i == 0,
                    0,
                )
            })
            .collect();
        let leg = LegSpec {
            dex: if cpmm { 2 } else { 5 },
            a_to_b: dir as u8,
            acct_start: 0,
            acct_len: infos.len() as u8,
        };
        run_leg(&leg, &infos, 123456789).unwrap();
        let calls = captured.lock().unwrap();
        let got = calls.last().unwrap();
        assert_eq!(got.program_id, keys[0]);
        assert_eq!(got.data, expected);
        assert_eq!(got.accounts.len(), metas.len());
        for (i, a) in got.accounts.iter().enumerate() {
            assert_eq!(a.pubkey, keys[i + 1]);
            assert_eq!(a.is_signer, metas[i]["signer"].as_bool().unwrap());
            assert_eq!(a.is_writable, metas[i]["writable"].as_bool().unwrap());
        }
        drop(calls);
        let mut bad = infos.clone();
        bad[0].key = &owner;
        assert_eq!(
            run_leg(&leg, &bad, 1),
            Err(ExecutorError::WrongDexProgram.into())
        );
        let mut range = leg;
        range.acct_len += 1;
        assert_eq!(
            run_leg(&range, &infos, 1),
            Err(ExecutorError::AccountRange.into())
        );
    }
    assert_eq!(captured.lock().unwrap().len(), 4);
    set_syscall_stubs(old);
}
