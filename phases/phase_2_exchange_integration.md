````markdown
# Phase 2 – Exchange Integration

This phase introduces Binance Spot API integration into CryptoGent.

The goal is to establish a reliable and secure exchange communication layer that supports:

- public Binance Spot endpoints
- signed Binance Spot endpoints
- API authentication
- request signing
- exchange metadata retrieval
- connectivity testing
- error normalization

This phase should **not** execute real trading logic yet beyond safe connectivity and exchange access verification.

---

# Phase Scope

This phase implements the following steps from the implementation roadmap:

6. Binance Spot API client  
7. request signing  
8. exchange rule retrieval  
9. API error handling  
10. connection testing  

---

# Core Objective

After completing Phase 2, CryptoGent should be able to:

- connect to Binance Spot Testnet and Mainnet
- authenticate using API key and secret
- send public API requests
- send signed API requests
- retrieve exchange metadata
- retrieve account information
- handle Binance API errors safely
- expose CLI commands for exchange diagnostics

---

# Binance Scope

CryptoGent must support **Binance Spot only**.

Supported scope:

- public market endpoints
- account endpoints
- spot order-related endpoints
- exchange metadata endpoints

Not supported:

- Futures
- Margin trading logic
- Withdrawals
- Borrowing
- Options
- Earn products

Even if Binance groups some permissions together, CryptoGent itself must remain **spot-only**.

---

# Environment Support

The exchange layer must support both:

```text
testnet
mainnet
````

Default should remain:

```text
testnet
```

Base URLs:

## Mainnet

```text
https://api.binance.com
```

## Testnet

```text
https://testnet.binance.vision
```

The selected environment must come from configuration.

---

# Required Configuration

The exchange integration must use configuration values like:

```yaml
exchange:
  name: binance
  mode: testnet
  timeout_seconds: 10
  retry_attempts: 3
  verify_ssl: true

  base_urls:
    mainnet: https://api.binance.com
    testnet: https://testnet.binance.vision
```

Sensitive values must come from environment variables:

```text
BINANCE_API_KEY
BINANCE_API_SECRET
```

---

# API Key Requirements

Recommended API key settings:

```text
Enable Reading: ON
Enable Spot & Margin Trading: ON
Enable Withdrawals: OFF
IP Restriction: optional
```

Notes:

* Withdrawals must remain disabled
* Spot & Margin Trading permission may be required by Binance UI even though CryptoGent will use Spot only
* Dynamic public IP may make IP whitelisting inconvenient for local development

---

# Exchange Client Module

Create a dedicated Binance Spot client module.

Suggested location:

```text
exchange/binance_client.py
```

Responsibilities:

* base URL management
* request construction
* signed request creation
* header injection
* query parameter handling
* timeout handling
* retries
* response parsing
* error normalization

This client should become the single entry point for Binance communication.

---

# Public Requests

Public requests do not require authentication.

Examples:

## Ping

```text
GET /api/v3/ping
```

## Server Time

```text
GET /api/v3/time
```

## Exchange Info

```text
GET /api/v3/exchangeInfo
```

## Ticker Price

```text
GET /api/v3/ticker/price
```

Public request support is required because later phases will depend on market and exchange metadata.

---

# Signed Requests

Signed requests require:

* API key header
* timestamp
* HMAC SHA256 signature

Examples:

## Account Info

```text
GET /api/v3/account
```

## Open Orders

```text
GET /api/v3/openOrders
```

## My Trades

```text
GET /api/v3/myTrades
```

Signed request support is required even in this phase so that connectivity and account access can be tested.

---

# Request Signing

Implement Binance signature generation using:

```text
HMAC SHA256(secret, query_string)
```

Signed requests must include:

* timestamp
* optional recvWindow
* signature

The API key must be attached through the request header.

Signature handling should be implemented in one reusable place only.

Suggested helper module:

```text
exchange/signer.py
```

---

# Exchange Rule Retrieval

Exchange metadata must be retrievable because later phases depend on:

* symbol status
* quote assets
* lot size
* tick size
* min notional
* step size

Source endpoint:

```text
GET /api/v3/exchangeInfo
```

Important future-use fields include:

* symbol
* status
* baseAsset
* quoteAsset
* filters

The exchange client should expose a normalized method to retrieve symbol rules.

---

# SSL and Connectivity Considerations

During local development, HTTPS/TLS issues may appear depending on:

* system SSL stack
* proxy settings
* VPN
* antivirus inspection
* router or ISP behavior

The client should support a configurable SSL verification setting:

```yaml
verify_ssl: true
```

For debugging only, allow temporary insecure mode.

Important rule:

* insecure mode must never be the production default
* insecure mode should only be used for local troubleshooting

---

# API Error Handling

The exchange layer must normalize Binance API errors into structured internal errors.

Common error scenarios:

## Invalid API key / permissions / IP

```text
HTTP 401
code: -2015
```

## Invalid order parameters

```text
code: -1013
```

## Timestamp outside recvWindow

```text
code: -1021
```

## Too many requests

```text
HTTP 429
```

## TLS / network failure

Examples:

* certificate verification failure
* timeout
* handshake failure

The client should return structured errors rather than raw exceptions whenever possible.

---

# Retry Strategy

Retry behavior should apply only to retry-safe scenarios.

Recommended retry cases:

* network timeout
* temporary exchange unavailability
* connection reset

Do not blindly retry on:

* authentication failure
* invalid order
* permission failure

Recommended config:

```yaml
timeout_seconds: 10
retry_attempts: 3
```

Use small backoff delays between retries.

---

# CLI Commands for Testing

This phase should expose exchange diagnostic commands through CLI.

Suggested commands:

## Ping

```text
python -m cryptogent exchange ping
```

## Server Time

```text
python -m cryptogent exchange time
```

## Exchange Info

```text
python -m cryptogent exchange info --symbol BTCUSDT
```

## Account

```text
python -m cryptogent exchange account
```

## Balances

```text
python -m cryptogent exchange balances
```

## Open Orders

```text
python -m cryptogent exchange open-orders
```

Optional debug command:

```text
python -m cryptogent exchange ping --insecure
```

---

# Connection Testing Goals

Connection testing in this phase must confirm:

* Binance host reachable
* public requests working
* signed requests working
* credentials loaded correctly
* permissions correct
* SSL/TLS behavior understood
* Testnet/Mainnet switching works

---

# Logging Requirements

All exchange operations must be logged.

Minimum log fields:

* timestamp
* component
* endpoint
* request type
* environment
* success/failure
* error message if any

Do not log:

* API secret
* request signature
* full sensitive headers

Optional safe logging:

* symbol
* endpoint path
* HTTP status
* Binance error code

---

# Module Suggestions

Suggested files for this phase:

```text
exchange/
  __init__.py
  binance_client.py
  signer.py
  errors.py
  models.py
  endpoints.py
```

Possible responsibilities:

## `binance_client.py`

* HTTP request execution
* environment selection
* public and signed request methods

## `signer.py`

* timestamp and signature creation

## `errors.py`

* normalized exception classes

## `models.py`

* typed response wrappers

## `endpoints.py`

* endpoint constants or helpers

---

# Deliverables

Phase 2 is complete when:

* Binance Spot client exists
* public endpoints work
* signed endpoints work
* request signing works
* exchange info can be retrieved
* account access can be tested
* errors are normalized
* CLI diagnostics are available

---

# Success Criteria

Phase 2 is successful when the system can:

* ping Binance successfully
* retrieve server time
* retrieve exchange info
* authenticate account requests
* show balances through CLI
* fail safely with structured errors when credentials or connectivity are invalid

```
```
