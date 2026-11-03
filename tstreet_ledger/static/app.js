(function () {
  "use strict";

  let latestState = null;
  let contractsCache = {};
  let activeContractName = "TStreetToken.sol";
  let selectedEip712Json = "";
  let toastTimer = null;

  function escapeHtml(value) {
    return String(value ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function shortHex(hex, prefixLen = 10, suffixLen = 6) {
    if (!hex || hex.length <= prefixLen + suffixLen + 2) return hex || "";
    return `${hex.slice(0, prefixLen)}…${hex.slice(-suffixLen)}`;
  }

  function showToast(message, isError = false) {
    const el = document.getElementById("toastBanner");
    if (!el) return;
    el.textContent = message;
    el.className = isError ? "toast error" : "toast";
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(() => {
      el.classList.add("hidden");
    }, 4500);
  }

  async function apiGet(path) {
    const resp = await fetch(path, { headers: { Accept: "application/json" } });
    const data = await resp.json();
    if (!resp.ok || data.ok === false) {
      throw new Error(data.error || `HTTP ${resp.status}`);
    }
    return data;
  }

  async function apiPost(path, body) {
    const resp = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(body || {}),
    });
    const data = await resp.json();
    if (!resp.ok || data.ok === false) {
      throw new Error(data.error || `HTTP ${resp.status}`);
    }
    return data;
  }

  function syncSelectOptions(selectEl, options, defaultIndex = 0) {
    if (!selectEl) return;
    const prevValue = selectEl.value;
    const currentSignature = JSON.stringify(options.map((o) => [o.value, o.label]));
    if (selectEl.dataset.sig === currentSignature) {
      return;
    }
    selectEl.dataset.sig = currentSignature;
    selectEl.innerHTML = options
      .map((o) => `<option value="${escapeHtml(o.value)}">${escapeHtml(o.label)}</option>`)
      .join("");
    if (prevValue && options.some((o) => o.value === prevValue)) {
      selectEl.value = prevValue;
    } else if (options[defaultIndex]) {
      selectEl.value = options[defaultIndex].value;
    }
  }

  function renderTelemetry(snap) {
    const ctrl = snap.control;
    const tok = snap.token;
    const led = snap.ledger;
    const inv = snap.invariants;

    const daemonBadge = document.getElementById("daemonBadge");
    const daemonBadgeText = document.getElementById("daemonBadgeText");
    if (tok.circuit_breaker_paused) {
      daemonBadge.className = "status-pill danger";
      daemonBadgeText.textContent = "Circuit Breaker Engaged";
    } else if (!ctrl.auto_sequence_enabled) {
      daemonBadge.className = "status-pill warn";
      daemonBadgeText.textContent = "Auto-Sequencer Paused (Manual Mode)";
    } else {
      daemonBadge.className = "status-pill ok";
      daemonBadgeText.textContent = "Control Loop Active";
    }

    const invBadge = document.getElementById("invariantBadge");
    const invText = document.getElementById("invariantBadgeText");
    if (inv.ok) {
      invBadge.className = "status-pill verified";
      invText.textContent = "Chain & Supply Verified";
    } else {
      invBadge.className = "status-pill danger";
      invText.textContent = "Invariant Alert";
    }

    document.getElementById("tickCounter").textContent = String(ctrl.tick_count);
    document.getElementById("tickIntervalLabel").textContent = `${ctrl.tick_interval_sec.toFixed(1)}s`;
    document.getElementById("stateRootShort").textContent = shortHex(led.latest_state_root, 12, 8);

    const toggleBtn = document.getElementById("toggleAutoSeqBtn");
    toggleBtn.textContent = ctrl.auto_sequence_enabled ? "Pause Auto-Sequencer" : "Resume Auto-Sequencer";

    const cbBtn = document.getElementById("circuitBreakerBtn");
    cbBtn.textContent = tok.circuit_breaker_paused ? "Clear Circuit Breaker" : "Engage Circuit Breaker";

    document.getElementById("kpiSupply").textContent = tok.total_supply_tst;
    document.getElementById("kpiMaxSupply").textContent = tok.max_supply_tst;
    document.getElementById("kpiBurned").textContent = tok.total_burned_tst;
    document.getElementById("kpiHeight").textContent = String(led.height);
    document.getElementById("kpiMempool").textContent = String(led.mempool_ready_count);
    document.getElementById("kpiEpoch").textContent = String(led.sequencer_epoch);

    document.getElementById("kpiEscrow").textContent = inv.escrow_locked_sum_tst;
    document.getElementById("kpiDealCount").textContent = String(snap.deals.length);
    document.getElementById("kpiDealFee").textContent = `${(tok.deal_fee_bps / 100).toFixed(2)}%`;

    document.getElementById("kpiSafeThreshold").textContent = `${snap.safe.threshold} / ${snap.safe.owner_count}`;
    document.getElementById("kpiFeesCollected").textContent = tok.total_fees_collected_tst;
    document.getElementById("kpiSafeNonce").textContent = String(snap.safe.safe_nonce);
    document.getElementById("walletFeeBpsLabel").textContent = `${tok.transfer_fee_bps} bps (${(tok.transfer_fee_bps / 100).toFixed(2)}%)`;
  }

  function renderSelectDropdowns(snap) {
    const walletOpts = snap.wallets.map((w) => ({
      value: w.address,
      label: `${w.label} (${w.balance_tst} TST)`,
    }));
    syncSelectOptions(document.getElementById("txSenderSelect"), walletOpts, 2);
    syncSelectOptions(document.getElementById("txRecipientSelect"), walletOpts, 3);
    syncSelectOptions(document.getElementById("burnSenderSelect"), walletOpts, 1);
    syncSelectOptions(document.getElementById("dealBuyerSelect"), walletOpts, 2);
    syncSelectOptions(document.getElementById("dealSellerSelect"), walletOpts, 3);
    syncSelectOptions(document.getElementById("safeMintRecipientSelect"), walletOpts, 1);

    const ownerOpts = snap.safe.owners.map((o) => ({
      value: o.address,
      label: `${o.label} — ${o.device}`,
    }));
    syncSelectOptions(document.getElementById("safeProposerSelect"), ownerOpts, 0);
  }

  function renderMempoolAndBlocks(snap) {
    const labelMap = {};
    snap.wallets.forEach((w) => {
      labelMap[w.address] = w.label;
    });

    const mempool = snap.ledger.ordered_mempool;
    document.getElementById("mempoolBadge").textContent = `${mempool.length} Ready`;
    const mpEl = document.getElementById("mempoolContainer");

    if (mempool.length === 0) {
      mpEl.className = "list-box empty-hint";
      mpEl.textContent =
        'No pending transactions in mempool. Click "Pause Auto-Sequencer" then "Simulate Priority Burst (3 Txs)" to watch live priority-fee ordering!';
    } else {
      mpEl.className = "list-box";
      mpEl.innerHTML = mempool
        .map(
          (tx, idx) => `
          <div class="item-card">
            <div class="item-top">
              <div>
                <span class="badge">#${idx + 1} IN EXECUTION ORDER</span>
                <span class="tx-pill">${escapeHtml(tx.tx_type)}</span>
              </div>
              <span class="mono">Priority Fee: <strong>${escapeHtml(tx.priority_fee_tst)} TST</strong></span>
            </div>
            <div style="margin-top:6px;font-size:12.5px;">
              <strong>${escapeHtml(labelMap[tx.sender] || shortHex(tx.sender))}</strong>
              → <strong>${escapeHtml(labelMap[tx.recipient] || shortHex(tx.recipient))}</strong>
              • <strong>${escapeHtml(tx.amount_tst)} TST</strong> (nonce ${tx.nonce})
            </div>
            ${tx.payload && tx.payload.memo ? `<div style="margin-top:4px;font-size:12px;color:var(--text-secondary);">${escapeHtml(tx.payload.memo)}</div>` : ""}
          </div>`
        )
        .join("");
    }

    const blocksEl = document.getElementById("blocksContainer");
    blocksEl.innerHTML = snap.ledger.blocks
      .map(
        (b) => `
        <div class="item-card">
          <div class="item-top">
            <div>
              <strong>Block #${b.height}</strong>
              <span class="badge">${b.tx_count} tx</span>
            </div>
            <code class="mono" style="font-size:11.5px;color:var(--text-secondary);">hash ${escapeHtml(shortHex(b.block_hash, 10, 6))}</code>
          </div>
          <div style="margin-top:5px;font-size:11.5px;color:var(--text-muted);" class="mono">
            prev ${escapeHtml(shortHex(b.prev_hash, 10, 6))} • merkle ${escapeHtml(shortHex(b.merkle_root, 10, 6))}
          </div>
          <div style="margin-top:8px;display:flex;flex-direction:column;gap:5px;">
            ${b.transactions
              .map(
                (tx) => `
                <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;font-size:12px;background:var(--bg-input);padding:5px 8px;border-radius:6px;">
                  <div>
                    <span class="tx-pill">${escapeHtml(tx.tx_type)}</span>
                    <span style="margin-left:6px;">${escapeHtml(tx.amount_tst)} TST</span>
                  </div>
                  <button type="button" class="tx-link-btn inspect-merkle-btn" data-txhash="${escapeHtml(tx.tx_hash)}">
                    Verify Proof (${escapeHtml(shortHex(tx.tx_hash, 8, 5))})
                  </button>
                </div>`
              )
              .join("")}
          </div>
        </div>`
      )
      .join("");

    const auditEl = document.getElementById("auditLogContainer");
    auditEl.innerHTML = snap.control.audit_log
      .slice(0, 25)
      .map(
        (ev) => `
        <div class="audit-row">
          <span class="audit-type">[${escapeHtml(ev.event_type)}]</span>
          <span>${escapeHtml(ev.message)}</span>
        </div>`
      )
      .join("");
  }

  function renderWallets(snap) {
    const tbody = document.getElementById("walletsTableBody");
    tbody.innerHTML = snap.wallets
      .map(
        (w) => `
        <tr>
          <td>
            <strong>${escapeHtml(w.label)}</strong>
            ${w.is_safe_owner ? '<span class="chain-tag" style="margin-left:6px;">Safe Signer</span>' : ""}
            ${w.fee_exempt ? '<span class="badge" style="margin-left:6px;">Fee-Exempt</span>' : ""}
          </td>
          <td class="mono">${escapeHtml(shortHex(w.address, 10, 6))}</td>
          <td class="mono"><strong>${escapeHtml(w.balance_tst)}</strong> TST</td>
          <td class="mono">${escapeHtml(w.escrow_locked_tst)} TST</td>
          <td class="mono">${w.confirmed_nonce}</td>
        </tr>`
      )
      .join("");
  }

  function renderDeals(snap) {
    const container = document.getElementById("dealsContainer");
    if (!snap.deals.length) {
      container.innerHTML = '<div class="empty-hint">No deals created yet.</div>';
      return;
    }
    container.innerHTML = snap.deals
      .map((d) => {
        let actions = "";
        if (d.state === "OPEN") {
          actions = `<button class="btn btn-primary btn-sm deal-act-btn" data-deal="${escapeHtml(d.deal_id)}" data-act="fund" data-caller="${escapeHtml(d.buyer)}">Fund Escrow (${escapeHtml(d.amount_tst)} TST)</button>`;
        } else if (d.state === "FUNDED") {
          actions = `
            <button class="btn btn-secondary btn-sm deal-act-btn" data-deal="${escapeHtml(d.deal_id)}" data-act="deliver" data-caller="${escapeHtml(d.seller)}">Mark Delivered (Seller)</button>
            <button class="btn btn-primary btn-sm deal-act-btn" data-deal="${escapeHtml(d.deal_id)}" data-act="release" data-caller="${escapeHtml(d.buyer)}">Release TST (Buyer)</button>
            <button class="btn btn-danger btn-sm deal-act-btn" data-deal="${escapeHtml(d.deal_id)}" data-act="dispute" data-caller="${escapeHtml(d.buyer)}">Dispute → Gnosis Safe</button>
          `;
        } else if (d.state === "DELIVERED") {
          actions = `
            <button class="btn btn-primary btn-sm deal-act-btn" data-deal="${escapeHtml(d.deal_id)}" data-act="release" data-caller="${escapeHtml(d.buyer)}">Approve &amp; Release TST</button>
            <button class="btn btn-danger btn-sm deal-act-btn" data-deal="${escapeHtml(d.deal_id)}" data-act="dispute" data-caller="${escapeHtml(d.buyer)}">Raise Dispute</button>
          `;
        } else if (d.state === "DISPUTED") {
          actions = `<span style="font-size:12px;color:#fcd34d;">Awaiting 2-of-3 Gnosis Safe arbitration in Tab 4 ("Gnosis Safe &amp; Ledger EIP-712")</span>`;
        }

        return `
          <div class="item-card">
            <div class="item-top">
              <div>
                <span class="mono" style="color:#38bdf8;font-weight:700;">${escapeHtml(d.deal_id)}</span>
                <strong style="margin-left:6px;">${escapeHtml(d.title)}</strong>
              </div>
              <span class="state-tag state-${escapeHtml(d.state)}">${escapeHtml(d.state)}</span>
            </div>
            <div style="margin-top:6px;font-size:12.5px;color:var(--text-secondary);">
              Buyer: <strong>${escapeHtml(d.buyer_label)}</strong> → Seller: <strong>${escapeHtml(d.seller_label)}</strong>
              • Principal: <strong>${escapeHtml(d.amount_tst)} TST</strong> (Escrow Locked: ${escapeHtml(d.escrow_locked_tst)} TST)
            </div>
            ${d.terms_summary ? `<div style="margin-top:5px;font-size:12px;color:var(--text-muted);">${escapeHtml(d.terms_summary)}</div>` : ""}
            ${d.dispute_reason ? `<div style="margin-top:5px;font-size:12px;color:#fca5a5;">Dispute: ${escapeHtml(d.dispute_reason)}</div>` : ""}
            ${d.resolution_note ? `<div style="margin-top:5px;font-size:12px;color:#6ee7b7;">${escapeHtml(d.resolution_note)}</div>` : ""}
            ${actions ? `<div class="card-actions">${actions}</div>` : ""}
          </div>`;
      })
      .join("");
  }

  function renderSafe(snap) {
    const safe = snap.safe;
    document.getElementById("safeAddressCode").textContent = safe.safe_address;
    document.getElementById("safeOwnersList").innerHTML = safe.owners
      .map(
        (o) => `
        <div class="owner-badge">
          <strong>${escapeHtml(o.label)}</strong><br/>
          <span class="mono" style="color:var(--text-muted);">${escapeHtml(shortHex(o.address, 8, 6))}</span> • ${escapeHtml(o.device)}
        </div>`
      )
      .join("");

    const pContainer = document.getElementById("safeProposalsContainer");
    if (!safe.proposals.length) {
      pContainer.innerHTML = '<div class="empty-hint">No Safe governance proposals yet.</div>';
      return;
    }

    if (!selectedEip712Json && safe.proposals[0]) {
      selectedEip712Json = JSON.stringify(safe.proposals[0].eip712_payload, null, 2);
      document.getElementById("eip712Preview").textContent = selectedEip712Json;
    }

    pContainer.innerHTML = safe.proposals
      .map((p) => {
        const signedSet = new Set(p.approvals.map((a) => a.signer));
        const signButtons =
          p.status === "PENDING"
            ? safe.owners
                .filter((o) => !signedSet.has(o.address))
                .map(
                  (o) => `
                  <button type="button" class="btn btn-primary btn-sm safe-sign-btn"
                    data-prop="${escapeHtml(p.proposal_id)}" data-signer="${escapeHtml(o.address)}">
                    Co-Sign with ${escapeHtml(o.label)}
                  </button>`
                )
                .join("")
            : "";

        return `
          <div class="item-card">
            <div class="item-top">
              <div>
                <span class="mono" style="color:#34d399;font-weight:700;">${escapeHtml(p.proposal_id)}</span>
                <span class="tx-pill" style="margin-left:6px;">${escapeHtml(p.action)}</span>
                <strong style="margin-left:6px;">${escapeHtml(p.title)}</strong>
              </div>
              <span class="state-tag state-${escapeHtml(p.status)}">${escapeHtml(p.status)} (${p.approval_count}/${p.threshold})</span>
            </div>
            <div style="margin-top:6px;font-size:12px;color:var(--text-secondary);" class="mono">
              Safe Nonce #${p.safe_nonce} • EIP-712 Digest: ${escapeHtml(shortHex(p.eip712_payload.meta.eip712SigningDigest, 14, 10))}
            </div>
            <div style="margin-top:6px;font-size:12px;color:var(--text-muted);">
              Signed by: ${p.approvals.map((a) => escapeHtml(a.label)).join(", ")}
            </div>
            <div class="card-actions">
              ${signButtons}
              <button type="button" class="btn btn-secondary btn-sm inspect-eip712-btn" data-prop="${escapeHtml(p.proposal_id)}">
                Inspect Ledger EIP-712 JSON
              </button>
            </div>
          </div>`;
      })
      .join("");
  }

  async function refreshState() {
    try {
      const resp = await apiGet("api/state");
      latestState = resp.data;
      renderTelemetry(latestState);
      renderSelectDropdowns(latestState);
      renderMempoolAndBlocks(latestState);
      renderWallets(latestState);
      renderDeals(latestState);
      renderSafe(latestState);
    } catch (err) {
      console.error("State poll error:", err);
    }
  }

  async function loadContracts() {
    try {
      const resp = await apiGet("api/contracts");
      contractsCache = resp.contracts || {};
      const viewer = document.getElementById("contractCodeViewer");
      viewer.textContent = contractsCache[activeContractName] || "// Contract not found";
    } catch (err) {
      document.getElementById("contractCodeViewer").textContent = `Error loading contracts: ${err.message}`;
    }
  }

  function bindEvents() {
    // Tab navigation
    document.querySelectorAll(".tab-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        document.querySelectorAll(".tab-btn").forEach((b) => b.classList.remove("active"));
        document.querySelectorAll(".tab-panel").forEach((p) => p.classList.remove("active"));
        btn.classList.add("active");
        const target = document.getElementById(`tab-${btn.dataset.tab}`);
        if (target) target.classList.add("active");
      });
    });

    // Top control bar actions
    document.getElementById("toggleAutoSeqBtn").addEventListener("click", async () => {
      if (!latestState) return;
      try {
        const nextVal = !latestState.control.auto_sequence_enabled;
        await apiPost("api/control/config", { auto_sequence_enabled: nextVal });
        showToast(nextVal ? "Auto-Sequencer resumed." : "Auto-Sequencer paused — mempool transactions will queue for inspection.");
        await refreshState();
      } catch (err) {
        showToast(err.message, true);
      }
    });

    document.getElementById("priorityBurstBtn").addEventListener("click", async () => {
      try {
        // Pause auto-sequencer briefly if user wants to see them in mempool, or enqueue directly
        const res = await apiPost("api/sequencer/demo-batch", {});
        const orderSummary = res.result.deterministic_execution_order
          .map((t, i) => `#${i + 1}: ${t.priority_fee_tst} TST fee (${t.amount_tst} TST)`)
          .join(" | ");
        showToast(`Priority burst ordered in mempool → ${orderSummary}`);
        await refreshState();
      } catch (err) {
        showToast(err.message, true);
      }
    });

    document.getElementById("sealNowBtn").addEventListener("click", async () => {
      try {
        const res = await apiPost("api/sequencer/seal", { allow_empty: false });
        if (res.result.sealed) {
          showToast(`Sealed Block #${res.result.block.height} (${res.result.block.tx_count} ordered transactions).`);
        } else {
          showToast("Mempool has no ready transactions to seal.");
        }
        await refreshState();
      } catch (err) {
        showToast(err.message, true);
      }
    });

    document.getElementById("circuitBreakerBtn").addEventListener("click", async () => {
      if (!latestState) return;
      try {
        const nextPaused = !latestState.token.circuit_breaker_paused;
        await apiPost("api/control/config", {
          circuit_breaker_paused: nextPaused,
          reason: "Operator manual circuit-breaker toggle",
        });
        showToast(nextPaused ? "Emergency circuit breaker ENGAGED." : "Circuit breaker cleared; ledger resumed.");
        await refreshState();
      } catch (err) {
        showToast(err.message, true);
      }
    });

    // Quick Transfer form
    document.getElementById("quickTransferForm").addEventListener("submit", async (ev) => {
      ev.preventDefault();
      try {
        const sealImm = document.getElementById("txSealImmediateCheck").checked;
        const res = await apiPost("api/transfers", {
          sender: document.getElementById("txSenderSelect").value,
          recipient: document.getElementById("txRecipientSelect").value,
          amount_tst: document.getElementById("txAmountInput").value,
          priority_fee_tst: document.getElementById("txPrioInput").value,
          memo: document.getElementById("txMemoInput").value,
          seal_immediately: sealImm,
        });
        showToast(
          sealImm
            ? `Transfer sealed in Block #${res.result.sealed_block.height}!`
            : `Transfer ${shortHex(res.result.tx.tx_hash)} queued in deterministic mempool.`
        );
        document.getElementById("txMemoInput").value = "";
        await refreshState();
      } catch (err) {
        showToast(err.message, true);
      }
    });

    // Register wallet
    document.getElementById("registerWalletForm").addEventListener("submit", async (ev) => {
      ev.preventDefault();
      try {
        const res = await apiPost("api/wallets/register", {
          label: document.getElementById("newWalletLabel").value,
          address: document.getElementById("newWalletAddr").value.trim(),
        });
        showToast(`Registered wallet "${res.result.label}" (${shortHex(res.result.address)}).`);
        document.getElementById("newWalletLabel").value = "";
        document.getElementById("newWalletAddr").value = "";
        await refreshState();
      } catch (err) {
        showToast(err.message, true);
      }
    });

    // Burn form
    document.getElementById("burnForm").addEventListener("submit", async (ev) => {
      ev.preventDefault();
      try {
        const res = await apiPost("api/burns", {
          sender: document.getElementById("burnSenderSelect").value,
          amount_tst: document.getElementById("burnAmountInput").value,
          reason: document.getElementById("burnReasonInput").value,
          seal_immediately: true,
        });
        showToast(`Burned ${res.result.tx.amount_tst} TST permanently.`);
        document.getElementById("burnReasonInput").value = "";
        await refreshState();
      } catch (err) {
        showToast(err.message, true);
      }
    });

    // Create deal form
    document.getElementById("createDealForm").addEventListener("submit", async (ev) => {
      ev.preventDefault();
      try {
        const res = await apiPost("api/deals/create", {
          title: document.getElementById("dealTitleInput").value,
          buyer: document.getElementById("dealBuyerSelect").value,
          seller: document.getElementById("dealSellerSelect").value,
          amount_tst: document.getElementById("dealAmountInput").value,
          expires_in_seconds: Number(document.getElementById("dealExpiryInput").value),
          terms_summary: document.getElementById("dealTermsInput").value,
        });
        showToast(`Created ${res.result.deal_id} (${res.result.amount_tst} TST).`);
        document.getElementById("dealTitleInput").value = "";
        document.getElementById("dealTermsInput").value = "";
        await refreshState();
      } catch (err) {
        showToast(err.message, true);
      }
    });

    // Safe action field switcher
    document.getElementById("safeActionSelect").addEventListener("change", (ev) => {
      const act = ev.target.value;
      document.getElementById("safeMintFields").classList.toggle("hidden", act !== "SAFE_MINT");
      document.getElementById("safeResolveFields").classList.toggle("hidden", act !== "RESOLVE_DEAL");
      document.getElementById("safeFeeFields").classList.toggle("hidden", act !== "SET_FEES");
    });

    // Propose Safe transaction
    document.getElementById("safeProposeForm").addEventListener("submit", async (ev) => {
      ev.preventDefault();
      try {
        const action = document.getElementById("safeActionSelect").value;
        let params = {};
        if (action === "SAFE_MINT") {
          params = {
            recipient: document.getElementById("safeMintRecipientSelect").value,
            amount_tst: document.getElementById("safeMintAmountInput").value,
          };
        } else if (action === "RESOLVE_DEAL") {
          params = {
            deal_id: document.getElementById("safeResolveDealId").value.trim(),
            seller_share_bps: Number(document.getElementById("safeResolveBps").value),
          };
        } else if (action === "SET_FEES") {
          params = {
            transfer_fee_bps: Number(document.getElementById("safeTransferFeeBps").value),
            deal_fee_bps: Number(document.getElementById("safeDealFeeBps").value),
          };
        }

        const res = await apiPost("api/safe/propose", {
          proposer: document.getElementById("safeProposerSelect").value,
          action,
          title: document.getElementById("safeTitleInput").value,
          params,
        });
        selectedEip712Json = JSON.stringify(res.result.eip712_payload, null, 2);
        document.getElementById("eip712Preview").textContent = selectedEip712Json;
        showToast(`Created Gnosis Safe proposal ${res.result.proposal_id} (1/2 Ledger signatures recorded).`);
        document.getElementById("safeTitleInput").value = "";
        await refreshState();
      } catch (err) {
        showToast(err.message, true);
      }
    });

    // Copy EIP-712 JSON
    document.getElementById("copyEip712Btn").addEventListener("click", async () => {
      if (!selectedEip712Json) return;
      try {
        await navigator.clipboard.writeText(selectedEip712Json);
        showToast("Copied canonical EIP-712 SafeTx payload to clipboard.");
      } catch (_) {
        showToast("Clipboard copy unavailable in this browser context.", true);
      }
    });

    // Contract switcher
    document.querySelectorAll(".contract-select-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        document.querySelectorAll(".contract-select-btn").forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        activeContractName = btn.dataset.contract;
        document.getElementById("contractCodeViewer").textContent =
          contractsCache[activeContractName] || "// Contract not loaded";
      });
    });

    // Delegated clicks: Merkle Proof, Deal actions, Safe co-signing
    document.body.addEventListener("click", async (ev) => {
      const merkleBtn = ev.target.closest(".inspect-merkle-btn");
      if (merkleBtn) {
        const txHash = merkleBtn.dataset.txhash;
        try {
          const resp = await apiGet(`api/tx/${encodeURIComponent(txHash)}`);
          const r = resp.result;
          const box = document.getElementById("merkleInspector");
          box.innerHTML = `
            <div style="color:#6ee7b7;font-weight:700;margin-bottom:6px;">
              MERKLE PROOF VERIFIED = ${r.merkle_verified ? "TRUE" : "FALSE"} (Block #${r.block_height}, Index ${r.tx_index})
            </div>
            <div class="mono" style="font-size:11.5px;word-break:break-all;">
              <div><strong>Tx Hash:</strong> ${escapeHtml(r.transaction.tx_hash)}</div>
              <div><strong>Block Merkle Root:</strong> ${escapeHtml(r.merkle_root)}</div>
              <div><strong>State Root:</strong> ${escapeHtml(r.state_root)}</div>
              <div style="margin-top:6px;"><strong>Sibling Audit Path (${r.merkle_proof.length} step${r.merkle_proof.length === 1 ? "" : "s"}):</strong></div>
              ${
                r.merkle_proof.length === 0
                  ? "<div>Single-leaf tree (leaf hash == root)</div>"
                  : r.merkle_proof
                      .map((s, i) => `<div>[${i}] ${escapeHtml(s.position.toUpperCase())}: ${escapeHtml(s.hash)}</div>`)
                      .join("")
              }
            </div>`;
        } catch (err) {
          showToast(err.message, true);
        }
        return;
      }

      const dealBtn = ev.target.closest(".deal-act-btn");
      if (dealBtn) {
        try {
          const act = dealBtn.dataset.act;
          const dealId = dealBtn.dataset.deal;
          const caller = dealBtn.dataset.caller;
          const res = await apiPost("api/deals/action", {
            action: act,
            deal_id: dealId,
            caller,
            reason: act === "dispute" ? "Escalated by counterparty for Gnosis Safe multisig review" : "",
          });
          showToast(`${dealId} updated → ${res.result.state}`);
          await refreshState();
        } catch (err) {
          showToast(err.message, true);
        }
        return;
      }

      const signBtn = ev.target.closest(".safe-sign-btn");
      if (signBtn) {
        try {
          const res = await apiPost("api/safe/approve", {
            proposal_id: signBtn.dataset.prop,
            signer: signBtn.dataset.signer,
          });
          showToast(
            `${res.result.proposal_id}: ${res.result.approval_count}/${res.result.threshold} signed → ${res.result.status}`
          );
          await refreshState();
        } catch (err) {
          showToast(err.message, true);
        }
        return;
      }

      const eipBtn = ev.target.closest(".inspect-eip712-btn");
      if (eipBtn && latestState) {
        const prop = latestState.safe.proposals.find((p) => p.proposal_id === eipBtn.dataset.prop);
        if (prop) {
          selectedEip712Json = JSON.stringify(prop.eip712_payload, null, 2);
          document.getElementById("eip712Preview").textContent = selectedEip712Json;
          showToast(`Loaded EIP-712 Ledger signing payload for ${prop.proposal_id}.`);
        }
      }
    });
  }

  bindEvents();
  refreshState();
  loadContracts();
  setInterval(refreshState, 2500);
})();
