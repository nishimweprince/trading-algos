//! Arg parsing against the fixture shared with the C++ encoder, and the guard math.
use jito_arb_executor::args::{ExecuteRouteArgs, LegSpec, ParseError, DATA_LEN};
use jito_arb_executor::guard::{meets_min_out, route_out};

fn unhex(s: &str) -> Vec<u8> {
    (0..s.len()).step_by(2).map(|i| u8::from_str_radix(&s[i..i + 2], 16).unwrap()).collect()
}

#[test]
fn parses_the_shared_fixture() {
    let body = include_str!("../../../tests/fixtures/execute_route.json");
    let v: serde_json::Value = serde_json::from_str(body).unwrap();
    let cases = v["cases"].as_array().unwrap();
    assert_eq!(cases.len(), 3);
    for c in cases {
        let bytes = unhex(c["hex"].as_str().unwrap());
        assert_eq!(bytes.len(), DATA_LEN);
        let a = ExecuteRouteArgs::parse(&bytes).unwrap();
        assert_eq!(a.amount_in, c["amount_in"].as_u64().unwrap());
        assert_eq!(a.min_out, c["min_out"].as_u64().unwrap());
        assert_eq!(a.num_legs as u64, c["num_legs"].as_u64().unwrap());
        for (i, l) in c["legs"].as_array().unwrap().iter().enumerate() {
            let l: Vec<u8> = l.as_array().unwrap().iter().map(|x| x.as_u64().unwrap() as u8).collect();
            assert_eq!(a.legs[i], LegSpec { dex: l[0], a_to_b: l[1], acct_start: l[2], acct_len: l[3] });
        }
    }
}

#[test]
fn rejects_bad_data() {
    let mut d = vec![0u8; DATA_LEN];
    d[17] = 2;
    assert!(ExecuteRouteArgs::parse(&d).is_ok());
    assert_eq!(ExecuteRouteArgs::parse(&d[..DATA_LEN - 1]), Err(ParseError::Length));
    d[0] = 1;
    assert_eq!(ExecuteRouteArgs::parse(&d), Err(ParseError::Tag));
    d[0] = 0;
    d[17] = 1;
    assert_eq!(ExecuteRouteArgs::parse(&d), Err(ParseError::LegCount));
    d[17] = 4;
    assert_eq!(ExecuteRouteArgs::parse(&d), Err(ParseError::LegCount));
}

#[test]
fn guard_math() {
    // Borrowed 1,000 into an ATA that held 50: pre = 1,050. The route returns 1,012: post = 1,062.
    assert_eq!(route_out(1_050, 1_062, 1_000), Some(1_012));
    assert!(meets_min_out(1_050, 1_062, 1_000, 1_012));
    assert!(!meets_min_out(1_050, 1_062, 1_000, 1_013)); // tampered min_out: the transaction reverts
    assert!(!meets_min_out(1_050, 1_040, 1_000, 1_000)); // returned 990: less than the 1,000 to repay
    assert_eq!(route_out(10, 5, 20), None); // impossible balance: never passes
    assert!(meets_min_out(u64::MAX, u64::MAX, u64::MAX, u64::MAX));
}
