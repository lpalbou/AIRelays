# Security

If you discover a security issue, do not publish sensitive exploit details in a public issue first.

Include:

- affected version or commit
- impact
- reproduction steps
- suggested mitigation if available

This project handles local auth state and traffic logs, so reports involving credential exposure, log leakage, or auth bypass are high priority.

High-priority classes include:

- relay bearer-token bypass
- upstream ChatGPT credential leakage
- log redaction failures
- rate-limit or temporary-block bypass
- accidental exposure of protected `/v1/*` routes
