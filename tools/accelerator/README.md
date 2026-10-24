# Mining accelerator 0.1.1

This tool collects RainbowMiner worker telemetry and the HashBurst pool summary.
It estimates workload performance from historical observations of the same worker
and hardware. Output is a proposed workload profile; it does not send commands,
change pool accounts, alter wallets, or install a full node.

Python 3.10 or later is required. The implementation uses the standard library.
Linux, macOS and Windows use the same source. The CI workflow tests this module on the three systems; it does not certify a full node.

## Run on Windows PowerShell

```powershell
$env:HB_RBMINER_URL = Read-Host 'RainbowMiner workers API URL'
python accelerator.py doctor
python accelerator.py collect --db history.sqlite
Copy-Item policy.example.json policy.json
python accelerator.py recommend --db history.sqlite --policy policy.json --out proposal-001.json
```

## Run on Linux, Raspberry Pi OS or macOS

```sh
read -r HB_RBMINER_URL
export HB_RBMINER_URL
python3 accelerator.py doctor
python3 accelerator.py collect --db history.sqlite
cp policy.example.json policy.json
python3 accelerator.py recommend --db history.sqlite --policy policy.json --out proposal-001.json
```

Collect at intervals of at least 60 seconds. A repeated source timestamp does not
create another training sample. Use a new proposal filename each time.
The database contains pseudonymous worker identifiers, normalized telemetry and a
local HMAC salt. It does not retain IP addresses, machine names, API credentials,
console output or raw API responses. Keep the database local to the coordinator.
Do not publish it as a release asset. Loss of the database also loses its worker
identifier namespace.

## Policy and model

Set `electricity_fiat_kwh`, `btc_fiat` and `price_observed_at` using actual operator
costs and a current exchange rate in the same fiat currency. The timestamp is Unix
seconds. No example rate is silently used. Different energy tariffs require
separate policy runs/databases until per-worker tariff binding is implemented.
Pool and algorithm allowlists must match the operator's intended deployments.
Temperature and switching thresholds are development defaults, not approval to
operate any particular device at those limits.

The model is an empirical mean and uncertainty margin for each observed workload
of each worker. It estimates BTC/day minus electricity at the supplied current
price. It uses the API's estimated revenue, not settled earnings or HBT rewards.
It requires distinct spaced samples, a minimum observation span, recent profiles,
matching hardware, permitted algorithms/pools and healthy telemetry. A candidate's
lower estimate must exceed the current profile's upper estimate by the configured
margin. These bounds are heuristics: correlated mining samples are not independent
experiments. They are not a guarantee of profit or a causal evaluation.

With only one observed profile, missing power, a stale price or insufficient
history, the result is `hold`. Benchmark and donation periods are excluded.
A later health alert prevents recommendations based on older healthy data.
Pool aggregates are hashed for provenance and counted, never distributed across
workers. Algorithms' hashrates are not added together.

No automatic exploration is performed. Historical counterfactual performance is
not inferred from another worker's earnings. To train alternatives, acquire
operator-approved benchmark runs and record their complete workload profiles.
The proposal includes workload, source sample time, model estimates, policy hash
and expiry. It is unsigned and `apply_supported` is false.

## Production integration still required

Bind the pseudonymous telemetry identity to the authenticated node identity.
Normalize owner-authorized pool worker APIs with explicit units and timestamps.
Add a reviewed RainbowMiner config adapter preserving pool user, payout wallet,
worker ID, API settings and locally reserved resources. Sign versioned proposals;
verify controller key, target node, expiry, monotonic version and local consent.
Use atomic configuration replacement, acknowledgement, canary acceptance and
rollback on stale telemetry or failed shares. Deliver only validated data, never
model-generated shell commands. Do not change GPU voltage, clocks or FPGA firmware
through this prototype.

CPU/storage contributions, TEP membership, HVM full-node status and validator
eligibility are separate facts. An API key does not establish consensus identity.
Faucet issuance requires accepted mainnet and its funded/rate-limited service.
Asset, utility and carbon-credit token allocations remain undefined until their
contracts and allocation rules are approved; this tool does not create claims.

## Validation

```sh
python3 -m unittest discover -s tests -v
```

Tests cover stale/invalid data, duplicate samples, redaction, missing economics,
health regression, profile selection, allowlists and refusal of execution mode.

## Temperature correction in 0.1.1

RainbowMiner's worker maxtemp is the configured notification threshold. Only
Devices[].Temp is treated as measured temperature. Zero or unavailable readings
are excluded. Existing database samples without the device_temp_v1 marker are
retained but excluded from recommendations. Collect fresh samples after upgrade;
previous proposals must not be used to diagnose actual overheating.
Source: https://github.com/RainbowMiner/RainbowMiner/issues/1416
