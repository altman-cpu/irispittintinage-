// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// @title TStreetToken (TST)
/// @notice Native ERC-20 + EIP-2612 token for T-Street Gnosis & Deal on Gnosis Chain (chainId 100).
/// @dev Governed by a Gnosis Safe (Safe{Wallet}) multisig backed by Ledger hardware signers.
///      Includes supply cap enforcement, emergency circuit-breaker pause, EIP-712 permit signatures,
///      2-step Safe governor rotation, and bounded protocol fee routing to the Safe treasury.
contract TStreetToken {
    string public constant name = "T-Street Gnosis Token";
    string public constant symbol = "TST";
    uint8 public constant decimals = 18;

    /// @notice Hard cap: 100,000,000 TST
    uint256 public constant MAX_SUPPLY = 100_000_000 * 1e18;

    /// @notice Maximum allowed transfer fee in basis points (5.00%)
    uint16 public constant MAX_FEE_BPS = 500;

    uint256 public totalSupply;
    address public safeGovernor;
    address public pendingSafeGovernor;
    address public safeTreasury;
    uint16 public transferFeeBps;
    bool public paused;

    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;
    mapping(address => uint256) public nonces;
    mapping(address => bool) public isFeeExempt;

    bytes32 private immutable _CACHED_DOMAIN_SEPARATOR;
    uint256 private immutable _CACHED_CHAIN_ID;
    bytes32 private constant _EIP712_DOMAIN_TYPEHASH =
        keccak256("EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)");
    bytes32 private constant _PERMIT_TYPEHASH =
        keccak256("Permit(address owner,address spender,uint256 value,uint256 nonce,uint256 deadline)");
    bytes32 private constant _NAME_HASH = keccak256(bytes("T-Street Gnosis Token"));
    bytes32 private constant _VERSION_HASH = keccak256(bytes("1"));

    event Transfer(address indexed from, address indexed to, uint256 value);
    event Approval(address indexed owner, address indexed spender, uint256 value);
    event GovernorTransferStarted(address indexed currentGovernor, address indexed pendingGovernor);
    event GovernorTransferred(address indexed previousGovernor, address indexed newGovernor);
    event TreasuryUpdated(address indexed previousTreasury, address indexed newTreasury);
    event TransferFeeUpdated(uint16 previousFeeBps, uint16 newFeeBps);
    event FeeExemptionUpdated(address indexed account, bool exempt);
    event PausedStateChanged(bool paused, address indexed triggeredBy);

    error UnauthorizedSafeGovernor();
    error ContractPaused();
    error ZeroAddress();
    error CapExceeded(uint256 requested, uint256 available);
    error InsufficientBalance(uint256 available, uint256 required);
    error InsufficientAllowance(uint256 available, uint256 required);
    error FeeTooHigh(uint16 requestedBps, uint16 maxBps);
    error PermitExpired(uint256 deadline, uint256 currentTimestamp);
    error InvalidSignature();

    modifier onlySafeGovernor() {
        if (msg.sender != safeGovernor) revert UnauthorizedSafeGovernor();
        _;
    }

    modifier whenNotPaused() {
        if (paused) revert ContractPaused();
        _;
    }

    /// @param initialSafeGovernor Gnosis Safe multisig address controlling mint/pause/parameters.
    /// @param initialSafeTreasury Gnosis Safe treasury receiving protocol fees and initial reserve.
    /// @param initialMintAmount Initial TST supply minted to `initialSafeTreasury` (in wei).
    /// @param initialFeeBps Optional transfer fee in basis points (0 - 500).
    constructor(
        address initialSafeGovernor,
        address initialSafeTreasury,
        uint256 initialMintAmount,
        uint16 initialFeeBps
    ) {
        if (initialSafeGovernor == address(0) || initialSafeTreasury == address(0)) revert ZeroAddress();
        if (initialFeeBps > MAX_FEE_BPS) revert FeeTooHigh(initialFeeBps, MAX_FEE_BPS);

        safeGovernor = initialSafeGovernor;
        safeTreasury = initialSafeTreasury;
        transferFeeBps = initialFeeBps;

        isFeeExempt[initialSafeGovernor] = true;
        isFeeExempt[initialSafeTreasury] = true;

        _CACHED_CHAIN_ID = block.chainid;
        _CACHED_DOMAIN_SEPARATOR = _computeDomainSeparator();

        if (initialMintAmount > 0) {
            _mint(initialSafeTreasury, initialMintAmount);
        }
    }

    function DOMAIN_SEPARATOR() public view returns (bytes32) {
        if (block.chainid == _CACHED_CHAIN_ID) {
            return _CACHED_DOMAIN_SEPARATOR;
        }
        return _computeDomainSeparator();
    }

    function approve(address spender, uint256 amount) external returns (bool) {
        if (spender == address(0)) revert ZeroAddress();
        allowance[msg.sender][spender] = amount;
        emit Approval(msg.sender, spender, amount);
        return true;
    }

    function transfer(address to, uint256 amount) external whenNotPaused returns (bool) {
        _transfer(msg.sender, to, amount);
        return true;
    }

    function transferFrom(address from, address to, uint256 amount) external whenNotPaused returns (bool) {
        uint256 allowed = allowance[from][msg.sender];
        if (allowed != type(uint256).max) {
            if (allowed < amount) revert InsufficientAllowance(allowed, amount);
            unchecked {
                allowance[from][msg.sender] = allowed - amount;
            }
            emit Approval(from, msg.sender, allowance[from][msg.sender]);
        }
        _transfer(from, to, amount);
        return true;
    }

    /// @notice EIP-2612 off-chain signature approval compatible with Ledger EIP-712 signing.
    function permit(
        address owner,
        address spender,
        uint256 value,
        uint256 deadline,
        uint8 v,
        bytes32 r,
        bytes32 s
    ) external {
        if (block.timestamp > deadline) revert PermitExpired(deadline, block.timestamp);
        if (owner == address(0) || spender == address(0)) revert ZeroAddress();

        bytes32 structHash = keccak256(abi.encode(_PERMIT_TYPEHASH, owner, spender, value, nonces[owner]++, deadline));
        bytes32 digest = keccak256(abi.encodePacked("\x19\x01", DOMAIN_SEPARATOR(), structHash));

        address recovered = ecrecover(digest, v, r, s);
        if (recovered == address(0) || recovered != owner) revert InvalidSignature();

        allowance[owner][spender] = value;
        emit Approval(owner, spender, value);
    }

    /// @notice Mint new TST up to MAX_SUPPLY (restricted to the Gnosis Safe governor).
    function mint(address to, uint256 amount) external onlySafeGovernor whenNotPaused {
        _mint(to, amount);
    }

    /// @notice Burn caller's TST tokens.
    function burn(uint256 amount) external whenNotPaused {
        _burn(msg.sender, amount);
    }

    /// @notice Burn TST from an approved account.
    function burnFrom(address from, uint256 amount) external whenNotPaused {
        uint256 allowed = allowance[from][msg.sender];
        if (allowed != type(uint256).max) {
            if (allowed < amount) revert InsufficientAllowance(allowed, amount);
            unchecked {
                allowance[from][msg.sender] = allowed - amount;
            }
            emit Approval(from, msg.sender, allowance[from][msg.sender]);
        }
        _burn(from, amount);
    }

    /// @notice Emergency circuit breaker controlled by the Gnosis Safe governor.
    function setPaused(bool shouldPause) external onlySafeGovernor {
        paused = shouldPause;
        emit PausedStateChanged(shouldPause, msg.sender);
    }

    function setTransferFeeBps(uint16 newFeeBps) external onlySafeGovernor {
        if (newFeeBps > MAX_FEE_BPS) revert FeeTooHigh(newFeeBps, MAX_FEE_BPS);
        uint16 old = transferFeeBps;
        transferFeeBps = newFeeBps;
        emit TransferFeeUpdated(old, newFeeBps);
    }

    function setFeeExempt(address account, bool exempt) external onlySafeGovernor {
        if (account == address(0)) revert ZeroAddress();
        isFeeExempt[account] = exempt;
        emit FeeExemptionUpdated(account, exempt);
    }

    function setSafeTreasury(address newTreasury) external onlySafeGovernor {
        if (newTreasury == address(0)) revert ZeroAddress();
        address old = safeTreasury;
        safeTreasury = newTreasury;
        isFeeExempt[newTreasury] = true;
        emit TreasuryUpdated(old, newTreasury);
    }

    function proposeSafeGovernor(address nextGovernor) external onlySafeGovernor {
        if (nextGovernor == address(0)) revert ZeroAddress();
        pendingSafeGovernor = nextGovernor;
        emit GovernorTransferStarted(safeGovernor, nextGovernor);
    }

    function acceptSafeGovernor() external {
        if (msg.sender != pendingSafeGovernor) revert UnauthorizedSafeGovernor();
        address previous = safeGovernor;
        safeGovernor = pendingSafeGovernor;
        pendingSafeGovernor = address(0);
        isFeeExempt[safeGovernor] = true;
        emit GovernorTransferred(previous, safeGovernor);
    }

    function _transfer(address from, address to, uint256 amount) internal {
        if (from == address(0) || to == address(0)) revert ZeroAddress();
        uint256 senderBal = balanceOf[from];
        if (senderBal < amount) revert InsufficientBalance(senderBal, amount);

        unchecked {
            balanceOf[from] = senderBal - amount;
        }

        uint256 fee = 0;
        if (transferFeeBps > 0 && !isFeeExempt[from] && !isFeeExempt[to]) {
            fee = (amount * transferFeeBps) / 10_000;
        }
        uint256 netAmount = amount - fee;

        balanceOf[to] += netAmount;
        emit Transfer(from, to, netAmount);

        if (fee > 0) {
            balanceOf[safeTreasury] += fee;
            emit Transfer(from, safeTreasury, fee);
        }
    }

    function _mint(address to, uint256 amount) internal {
        if (to == address(0)) revert ZeroAddress();
        uint256 remaining = MAX_SUPPLY - totalSupply;
        if (amount > remaining) revert CapExceeded(amount, remaining);

        totalSupply += amount;
        balanceOf[to] += amount;
        emit Transfer(address(0), to, amount);
    }

    function _burn(address from, uint256 amount) internal {
        uint256 bal = balanceOf[from];
        if (bal < amount) revert InsufficientBalance(bal, amount);
        unchecked {
            balanceOf[from] = bal - amount;
            totalSupply -= amount;
        }
        emit Transfer(from, address(0), amount);
    }

    function _computeDomainSeparator() internal view returns (bytes32) {
        return keccak256(
            abi.encode(_EIP712_DOMAIN_TYPEHASH, _NAME_HASH, _VERSION_HASH, block.chainid, address(this))
        );
    }
}
