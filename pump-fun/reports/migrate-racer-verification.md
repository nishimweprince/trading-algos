# Migration-racer verification (spec §7)

Generated: 2026-10-04T22:51:25.132Z
Params: maxMigrates=1000 probeTop=5 probeDepth=100 slotWindow=150

## Sample

- Migrates confirmed: 1000
- Signatures scanned: 2000 over 2 page(s)
- Fetched: 1573, fetch failed: 0
- Failed txs skipped: 427, non-migrate touching authority: 72

## Caller ranking (top 10)

| Caller | Migrates | Net of fee (SOL) | Net-positive | Token gains (caller-owned) |
| --- | --- | --- | --- | --- |
| 9C4nRvhhVquCKATjDCx5FKvNS9PNgNqgyWy9AcoDjYv5 | 464 | -1.000923 | 0/464 | none |
| Gygj9QQby4j2jryqyqBHvLP7ctv2SaANgh4sCb69BUpA | 84 | -0.021005 | 0/84 | none |
| 92CZnNR1HgGVAFf8UGeVgK6LGQunRaPYQDVuJ7KAFbsn | 32 | -0.047630 | 0/32 | none |
| 3RnALvN6YRyb8EBU8BGepnYNDpnA6ZENBfoxB8MS73oQ | 26 | -0.038699 | 0/26 | none |
| F7LqQscxh7NK8S3AJaEZ7vryxvQbqKksQAZKtVgJYwmE | 24 | -0.079026 | 0/24 | 6sVQpyXu… 4741977938399; So111111… -57509230191; Ec22aivh… 6970027084111; G939riCW… 6970027083952; 3tes5VMg… 6970027083952; 2BFGrFF7… 6970027084032; 55z3QZQn… 6970027084111; 62Vq3acG… 6970027084111; 8XKeJ1fF… 6970027084032; H3HxwHJk… 3630704599735; 87TbQkkv… 5575201034246; 4jJiB7Fz… 6970027084032; AFPqvARp… 6952681642082; 9Yt46do4… 6970027084032; BK77PdW4… 5562596891192; AYXqgh4R… 6970027084190; y8mSbDVM… 639887520523; GWVuTdm4… 1591275058099; Fw57o4rn… 6970027084032; 2WMnisqb… 6970027084032; 389GhGCf… 6970027084032; Bh7T3PZT… 6970027084032; Cvfx1WcW… 639887520523; F5pFhuAd… 6970027084032 |
| H2ckWrvX2UcC3a74mkd9jcpvr1tESo27Rjgz16uGWtF5 | 22 | -0.032746 | 0/22 | none |
| FEUa5TK22AyRyyjKpd2bCx7se1Eczmt7AFxdS6dUfHz4 | 17 | -0.035303 | 0/17 | 5FtuM5g1… 6970027083952; So111111… -36000000000; DJGsptd7… 6970027084032; 71B9WX3C… 6970027084032; 9EatdT2N… 6970027084032; GWauJGtR… 6970027084111; DhQFJXJE… 6970027083873; 35Vy8wqT… 6970027084190; 9TbHZ1Kk… 6970027084032; ALd1KJ72… 6970027083952; 6mJFyMpr… 6970027084032; GVHuvtuh… 6970027083952; 3EMec5v2… 6970027084032 |
| FHpcNSe6tb2n15bAdq4BkeYWGyZKFD7yLYrH92ng7wCT | 13 | -0.007064 | 0/13 | EPjFWdd5… 2373375 |
| 9zAWADf2cWdqJaco6VduFQYF6vxqTcyXzWoLnM7ziUoy | 12 | -0.017861 | 0/12 | none |
| 4YvpGfaqG2nB8J3tFj6JD9QpmRRZqTTEXkNTkWSpDrQJ | 10 | -0.328766 | 0/10 | 2rEj69iV… 3776721696785; So111111… -14200000000; GaGoBQFr… 3312189007469; 8W8hXBka… 3312189007508; 2qNBJuME… 3312189007508; 4wwXaBKt… 3312189007469; EpjcvRPP… 3312189007431; 3sfF6Vyr… 3312189007661; J2EuZcNM… 3223722572414; ALd1KJ72… 3220245301756; 2fpF5XAU… 3312189007508 |

## Caller probes (spec §7 steps 3–5)

### 9C4nRvhhVquCKATjDCx5FKvNS9PNgNqgyWy9AcoDjYv5 (won 464, history scanned 100)

- Any buy activity: no
- Completion buys (≤150 slots before): 0
- Post-migrate buys (≤150 slots after): 0
- Incentive-claim txs: 0

### Gygj9QQby4j2jryqyqBHvLP7ctv2SaANgh4sCb69BUpA (won 84, history scanned 95)

- Any buy activity: yes
- Completion buys (≤150 slots before): 0
- Post-migrate buys (≤150 slots after): 0
- Incentive-claim txs: 0

### 92CZnNR1HgGVAFf8UGeVgK6LGQunRaPYQDVuJ7KAFbsn (won 32, history scanned 100)

- Any buy activity: yes
- Completion buys (≤150 slots before): 6
  - HmFhFSgi4X76ZmnH8F9WQvWY4dAuv4mj1dQGmnPxqhZC 0 slots before (oU4vpjz9L79GxhsX…)
  - BkhNaZuULsotS54desFdfTefmFj4zXKfw4425h1Jdp1H 0 slots before (4MDKeRPtuDQojq9n…)
  - 7MkURjNDL9UAay7XVXsqrdoKu3DiiYki6ZknfRNLMUQh 0 slots before (4n1f3u9DnunNGTMw…)
  - Hjs1ewbMYzsdUs2QhPvY2tx7o4twGsJHyzkpipToBbty 0 slots before (3ubTkHLCJH3wxtij…)
  - AqHnbuG4ujo4DqtN686GJujiRfBdpab97MYuGJ2dc4Bx 0 slots before (3kBDAS1LpkzwDVCp…)
  - GDrwKbnKfLvjyFnvJajhDTZWtHJdWXRa1sxBXMwmpetp 0 slots before (2w1Ei6tH1cHmKbqR…)
- Post-migrate buys (≤150 slots after): 0
- Incentive-claim txs: 0

### 3RnALvN6YRyb8EBU8BGepnYNDpnA6ZENBfoxB8MS73oQ (won 26, history scanned 100)

- Any buy activity: yes
- Completion buys (≤150 slots before): 6
  - H9D6mNL5tmJVkej8tYeuHeGqHPPhcrjoZYWfXvWLeL6e 0 slots before (1idpG1tXg8cbPaq3…)
  - En3QXSyDyAHK693cRgaLj1gbSDZVkZFdHj3a87GruiwU 0 slots before (3pKq1KkWcHCt9F2r…)
  - 8xU4KJPKYcVpxcdQ894nH6B59ZsvePFRBZgc58mgj8JB 0 slots before (5AzxzVuLMBtqTwMN…)
  - 3VnKetJZTkUFEQv5ixtSLWanZVAEspm7C3dBXRx6rJww 0 slots before (UN2Dyt4zrw88HeQ1…)
  - 4FpBgHpWZbr6Gtfr4USMbathiFFNuF7jAcHF5W3C87kr 0 slots before (3KUht3dYRkxkWSxD…)
  - GbXnEiqMjXe5ck9HyX3N9rLqAfYzYhy5SX6GEoFxGbma 0 slots before (3tYDo4uYFasQgPim…)
- Post-migrate buys (≤150 slots after): 0
- Incentive-claim txs: 0

### F7LqQscxh7NK8S3AJaEZ7vryxvQbqKksQAZKtVgJYwmE (won 24, history scanned 92)

- Any buy activity: yes
- Completion buys (≤150 slots before): 1
  - 6sVQpyXusu7Ej9zPfV3LSrLGNG5QVR6AZMTgcivUpump 0 slots before (21TEFSf5TSGsg6qB…)
- Post-migrate buys (≤150 slots after): 0
- Incentive-claim txs: 0

## Hypothesis scoreboard (raw counts — see analysis before concluding)

- Migrates with caller net-of-fee > 0: 0/1000 (caller-reward support)
- Callers with caller-owned token gains: 7 (LP/payout-token support)
- Completion buys found in probes: 13 (push-over-the-line support)
- Post-migrate buys found in probes: 0 (first-position support)
- Incentive claims found in probes: 0 (volume-incentive support)

Caveats: RPC cannot prove bundle membership — slot proximity is reported, not bundles.
Fee-payer attribution assumes the migrate caller pays the transaction fee.