# T-Street Gnosis & Deal — Ordered Ledger, `TST` Token & Continuous Control Plane

This module implements the **T-Street Gnosis & Deal** cryptocurrency, deterministic ordered ledger, 2-of-3 **Gnosis Safe (`Safe{Wallet}`)** governance layer, bilateral **Deal Escrow** engine, and self-hosted **Continuous Control Plane**.

---

## 1. Ordering & Initializing a Physical Ledger Hardware Wallet

> **Security Note**: Automated agents cannot and should not sign up for third-party accounts, submit payment/shipping details, or generate hardware seed phrases on your behalf. Always order hardware wallets directly from the manufacturer and initialize them offline.

1. **Order Hardware Signers Directly**:
   - Order 2–3 hardware devices (e.g., Ledger Flex / Nano X) directly from the official manufacturer (`shop.ledger.com`) — never from third-party resellers.
2. **Initialize Offline & Install the Ethereum / Gnosis App**:
   - Generate your 24-word recovery phrase offline on each physical device and install the **Ethereum** / **Gnosis** app via Ledger Live.
3. **Create Your 2-of-3 Gnosis Safe on Gnosis Chain (`chainId: 100`)**:
   - Open [`https://app.safe.global`](https://app.safe.global), select **Gnosis Chain** (or **Gnosis Chiado Testnet**, `chainId: 10200` for staging), connect your Ledger devices, and deploy a **2-of-3 `Safe{Wallet}`**.
   - Record your deployed Safe address — it serves as `initialSafeGovernor`, `initialSafeTreasury`, and `initialSafeArbiter` in the T-Street smart contracts.

---

## 2. Smart Contracts (`tstreet_ledger/contracts/`)

Both contracts are written in self-contained Solidity `^0.8.24` with zero external package dependencies:

| Contract | Purpose | Key Safety Controls |
| --- | --- | --- |
| `TStreetToken.sol` (`TST`) | 18-decimal ERC-20 + EIP-2612 `permit` token for Gnosis Chain (`100`) | `100,000,000 TST` hard cap, 2-step `safeGovernor` transfer, emergency `paused` circuit breaker, bounded `transferFeeBps` (`<= 500 bps`) routed to `safeTreasury` |
| `TStreetDealEscrow.sol` | Bilateral OTC & milestone Deal Escrow denominated in `TST` | Reentrancy guard, `Open -> Funded -> Delivered -> Released` lifecycle, `raiseDispute` escalation to `safeArbiter`, split/refund arbitration (`resolveDisputeBySafe`), expiry refunds (`claimExpiredRefund`), bounded `dealFeeBps` (`<= 250 bps`) |

### Deploying to Gnosis Chain (`100`) or Chiado (`10200`) with Foundry

```bash
# 1. Deploy TStreetToken (initial mint: 20,000,000 TST = 20000000000000000000000000 wei, transfer fee: 10 bps)
forge create tstreet_ledger/contracts/TStreetToken.sol:TStreetToken \
  --rpc-url https://rpc.gnosischain.com \
  --ledger \
  --constructor-args <YOUR_GNOSIS_SAFE_ADDRESS> <YOUR_GNOSIS_SAFE_ADDRESS> 20000000000000000000000000 10

# 2. Deploy TStreetDealEscrow (deal settlement fee: 25 bps)
forge create tstreet_ledger/contracts/TStreetDealEscrow.sol:TStreetDealEscrow \
  --rpc-url https://rpc.gnosischain.com \
  --ledger \
  --constructor-args <DEPLOYED_TST_ADDRESS> <YOUR_GNOSIS_SAFE_ADDRESS> <YOUR_GNOSIS_SAFE_ADDRESS> 25
```

After deployment, submit a Safe transaction calling `setFeeExempt(<DEPLOYED_ESCROW_ADDRESS>, true)` on `TStreetToken` so escrow lockups are exempt from transfer fees.

---

## 3. Running the Continuous Control Plane & Ordered Ledger

GitHub Actions is designed for CI/CD verification (`.github/workflows/tstreet-ledger-ci.yml`) rather than perpetual background daemons (running infinite loops or mining on GitHub runners violates GitHub's Terms of Service). To run **24/7 non-stop sequencing, expiry sweeps, and invariant monitoring**, host `tstreet_ledger.server` on your own server or container:

```bash
python3 -m tstreet_ledger.server --host 0.0.0.0 --port 8080 --tick-sec 3.0
```

### Example `systemd` Service (`/etc/systemd/system/tstreet-control.service`)

```ini
[Unit]
Description=T-Street Gnosis & Deal Ordered Ledger & Control Plane
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/irispittintinage-
ExecStart=/usr/bin/python3 -m tstreet_ledger.server --host 0.0.0.0 --port 8080 --tick-sec 3.0
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

### What the Continuous Control Loop Does Every Tick

1. **Deterministic Mempool Sequencing**: Orders ready transactions by `(sender_nonce, -priority_fee_wei, created_ns, tx_hash)`, executes state transitions, computes the SHA-256 Merkle root + global state root, and seals the next hash-chained block.
2. **Automated Expired-Deal Sweeps**: Refunds `FUNDED` deals that pass their expiry deadline without delivery or dispute.
3. **Cryptographic & Supply Watchdog**: Re-verifies full chain linkage, Merkle roots, nonce monotonicity, and exact `1 wei` supply conservation (`sum(wallets) + sum(escrows) == total_supply <= 100,000,000 TST`), automatically engaging the emergency circuit breaker if any invariant ever fails.
