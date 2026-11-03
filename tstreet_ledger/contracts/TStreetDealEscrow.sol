// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

interface IERC20Minimal {
    function transfer(address to, uint256 amount) external returns (bool);
    function transferFrom(address from, address to, uint256 amount) external returns (bool);
}

/// @title TStreetDealEscrow
/// @notice Bilateral & milestone escrow settlement contract for T-Street Gnosis & Deal.
/// @dev Designed for Gnosis Chain (chainId 100) with a Gnosis Safe (Safe{Wallet}) multisig
///      acting as protocol arbiter and fee treasury.
contract TStreetDealEscrow {
    enum DealState {
        None,
        Open,
        Funded,
        Delivered,
        Released,
        Disputed,
        Refunded,
        Cancelled
    }

    struct Deal {
        uint256 dealId;
        address buyer;
        address seller;
        uint256 amount;
        uint64 createdAt;
        uint64 expiresAt;
        bytes32 termsHash;
        DealState state;
    }

    /// @notice Maximum protocol settlement fee: 2.50% (250 bps)
    uint16 public constant MAX_DEAL_FEE_BPS = 250;

    IERC20Minimal public immutable tstToken;
    address public safeArbiter;
    address public safeTreasury;
    uint16 public dealFeeBps;
    bool public paused;
    uint256 public nextDealId = 1;

    uint256 private _locked = 1;
    mapping(uint256 => Deal) public deals;

    event DealCreated(
        uint256 indexed dealId,
        address indexed buyer,
        address indexed seller,
        uint256 amount,
        uint64 expiresAt,
        bytes32 termsHash
    );
    event DealFunded(uint256 indexed dealId, address indexed buyer, uint256 amount);
    event DealDelivered(uint256 indexed dealId, address indexed seller);
    event DealReleased(uint256 indexed dealId, address indexed seller, uint256 netPayout, uint256 fee);
    event DealDisputed(uint256 indexed dealId, address indexed initiator);
    event DealResolvedBySafe(
        uint256 indexed dealId,
        bool releasedToSeller,
        uint256 sellerPayout,
        uint256 buyerRefund,
        uint256 fee
    );
    event DealRefunded(uint256 indexed dealId, address indexed buyer, uint256 amount);
    event DealCancelled(uint256 indexed dealId);
    event SafeArbiterUpdated(address indexed previousArbiter, address indexed newArbiter);
    event DealFeeUpdated(uint16 previousFeeBps, uint16 newFeeBps);
    event PausedStateChanged(bool paused);

    error ReentrancyBlocked();
    error Unauthorized();
    error ContractPaused();
    error ZeroAddress();
    error InvalidAmount();
    error InvalidExpiry();
    error InvalidDealState(DealState current, DealState expected);
    error DealNotExpired(uint64 expiresAt, uint64 currentTimestamp);
    error FeeTooHigh(uint16 requested, uint16 maxAllowed);
    error TokenTransferFailed();

    modifier nonReentrant() {
        if (_locked != 1) revert ReentrancyBlocked();
        _locked = 2;
        _;
        _locked = 1;
    }

    modifier onlySafeArbiter() {
        if (msg.sender != safeArbiter) revert Unauthorized();
        _;
    }

    modifier whenNotPaused() {
        if (paused) revert ContractPaused();
        _;
    }

    constructor(
        address tokenAddress,
        address initialSafeArbiter,
        address initialSafeTreasury,
        uint16 initialDealFeeBps
    ) {
        if (
            tokenAddress == address(0) ||
            initialSafeArbiter == address(0) ||
            initialSafeTreasury == address(0)
        ) revert ZeroAddress();
        if (initialDealFeeBps > MAX_DEAL_FEE_BPS) {
            revert FeeTooHigh(initialDealFeeBps, MAX_DEAL_FEE_BPS);
        }

        tstToken = IERC20Minimal(tokenAddress);
        safeArbiter = initialSafeArbiter;
        safeTreasury = initialSafeTreasury;
        dealFeeBps = initialDealFeeBps;
    }

    /// @notice Create a new T-Street deal record prior to escrow funding.
    function createDeal(
        address seller,
        uint256 amount,
        uint64 expiresAt,
        bytes32 termsHash
    ) external whenNotPaused returns (uint256 dealId) {
        if (seller == address(0) || seller == msg.sender) revert ZeroAddress();
        if (amount == 0) revert InvalidAmount();
        if (expiresAt <= block.timestamp) revert InvalidExpiry();

        dealId = nextDealId++;
        deals[dealId] = Deal({
            dealId: dealId,
            buyer: msg.sender,
            seller: seller,
            amount: amount,
            createdAt: uint64(block.timestamp),
            expiresAt: expiresAt,
            termsHash: termsHash,
            state: DealState.Open
        });

        emit DealCreated(dealId, msg.sender, seller, amount, expiresAt, termsHash);
    }

    /// @notice Buyer locks TST into escrow for an Open deal.
    function fundDeal(uint256 dealId) external nonReentrant whenNotPaused {
        Deal storage d = deals[dealId];
        if (msg.sender != d.buyer) revert Unauthorized();
        if (d.state != DealState.Open) revert InvalidDealState(d.state, DealState.Open);
        if (block.timestamp >= d.expiresAt) revert InvalidExpiry();

        d.state = DealState.Funded;
        _safeTransferFrom(d.buyer, address(this), d.amount);
        emit DealFunded(dealId, d.buyer, d.amount);
    }

    /// @notice Seller marks deal deliverables complete.
    function markDelivered(uint256 dealId) external whenNotPaused {
        Deal storage d = deals[dealId];
        if (msg.sender != d.seller) revert Unauthorized();
        if (d.state != DealState.Funded) revert InvalidDealState(d.state, DealState.Funded);

        d.state = DealState.Delivered;
        emit DealDelivered(dealId, d.seller);
    }

    /// @notice Buyer approves settlement, releasing TST to seller (minus protocol fee to Safe treasury).
    function approveRelease(uint256 dealId) external nonReentrant whenNotPaused {
        Deal storage d = deals[dealId];
        if (msg.sender != d.buyer) revert Unauthorized();
        if (d.state != DealState.Funded && d.state != DealState.Delivered) {
            revert InvalidDealState(d.state, DealState.Delivered);
        }

        d.state = DealState.Released;
        uint256 fee = (d.amount * dealFeeBps) / 10_000;
        uint256 sellerPayout = d.amount - fee;

        _safeTransfer(d.seller, sellerPayout);
        if (fee > 0) {
            _safeTransfer(safeTreasury, fee);
        }

        emit DealReleased(dealId, d.seller, sellerPayout, fee);
    }

    /// @notice Either buyer or seller escalates a Funded/Delivered deal to the Gnosis Safe arbiter.
    function raiseDispute(uint256 dealId) external {
        Deal storage d = deals[dealId];
        if (msg.sender != d.buyer && msg.sender != d.seller) revert Unauthorized();
        if (d.state != DealState.Funded && d.state != DealState.Delivered) {
            revert InvalidDealState(d.state, DealState.Funded);
        }

        d.state = DealState.Disputed;
        emit DealDisputed(dealId, msg.sender);
    }

    /// @notice Gnosis Safe multisig resolves a disputed deal (supports full or split settlement in bps).
    /// @param sellerShareBps Portion awarded to seller in basis points (0 = full buyer refund, 10000 = full seller release).
    function resolveDisputeBySafe(uint256 dealId, uint16 sellerShareBps) external nonReentrant onlySafeArbiter {
        if (sellerShareBps > 10_000) revert FeeTooHigh(sellerShareBps, 10_000);
        Deal storage d = deals[dealId];
        if (d.state != DealState.Disputed) revert InvalidDealState(d.state, DealState.Disputed);

        uint256 grossSeller = (d.amount * sellerShareBps) / 10_000;
        uint256 buyerRefund = d.amount - grossSeller;
        uint256 fee = (grossSeller * dealFeeBps) / 10_000;
        uint256 netSeller = grossSeller - fee;

        d.state = sellerShareBps > 0 ? DealState.Released : DealState.Refunded;

        if (netSeller > 0) _safeTransfer(d.seller, netSeller);
        if (buyerRefund > 0) _safeTransfer(d.buyer, buyerRefund);
        if (fee > 0) _safeTransfer(safeTreasury, fee);

        emit DealResolvedBySafe(dealId, sellerShareBps > 0, netSeller, buyerRefund, fee);
    }

    /// @notice Buyer reclaims escrowed TST if deal expired without delivery or dispute.
    function claimExpiredRefund(uint256 dealId) external nonReentrant {
        Deal storage d = deals[dealId];
        if (msg.sender != d.buyer) revert Unauthorized();
        if (d.state != DealState.Funded) revert InvalidDealState(d.state, DealState.Funded);
        if (block.timestamp < d.expiresAt) revert DealNotExpired(d.expiresAt, uint64(block.timestamp));

        d.state = DealState.Refunded;
        _safeTransfer(d.buyer, d.amount);
        emit DealRefunded(dealId, d.buyer, d.amount);
    }

    /// @notice Buyer cancels an unfunded Open deal.
    function cancelOpenDeal(uint256 dealId) external {
        Deal storage d = deals[dealId];
        if (msg.sender != d.buyer) revert Unauthorized();
        if (d.state != DealState.Open) revert InvalidDealState(d.state, DealState.Open);

        d.state = DealState.Cancelled;
        emit DealCancelled(dealId);
    }

    function setSafeArbiter(address newArbiter) external onlySafeArbiter {
        if (newArbiter == address(0)) revert ZeroAddress();
        address prev = safeArbiter;
        safeArbiter = newArbiter;
        emit SafeArbiterUpdated(prev, newArbiter);
    }

    function setDealFeeBps(uint16 newFeeBps) external onlySafeArbiter {
        if (newFeeBps > MAX_DEAL_FEE_BPS) revert FeeTooHigh(newFeeBps, MAX_DEAL_FEE_BPS);
        uint16 prev = dealFeeBps;
        dealFeeBps = newFeeBps;
        emit DealFeeUpdated(prev, newFeeBps);
    }

    function setPaused(bool shouldPause) external onlySafeArbiter {
        paused = shouldPause;
        emit PausedStateChanged(shouldPause);
    }

    function _safeTransfer(address to, uint256 amount) internal {
        bool ok = tstToken.transfer(to, amount);
        if (!ok) revert TokenTransferFailed();
    }

    function _safeTransferFrom(address from, address to, uint256 amount) internal {
        bool ok = tstToken.transferFrom(from, to, amount);
        if (!ok) revert TokenTransferFailed();
    }
}
