# Security Policy

## Supported Versions

Only the latest stable release receives security updates. Older versions and pre-release versions (rc, beta, alpha) are not covered by this policy.

## Reporting a Vulnerability

If you discover a security vulnerability in Eneru, please report it responsibly:

1. **Do NOT open a public issue.** Security vulnerabilities must be reported privately.

2. **Use GitHub's private vulnerability reporting:**
   Go to the [Security Advisories](https://github.com/m4r1k/Eneru/security/advisories) page and click "Report a vulnerability."

3. **What to include:**
   - Description of the vulnerability
   - Steps to reproduce
   - Potential impact
   - Suggested fix (if any)

4. **Response time:** You can expect an initial response within 7 days. A fix or mitigation plan will be communicated within 30 days of the report.

## Scope

Eneru is a UPS monitoring daemon that executes shutdown commands on critical infrastructure. Security issues of particular concern include:

- Command injection via configuration values or UPS data
- Unauthorized access to shutdown functionality
- Path traversal in file operations
- Information disclosure of sensitive configuration (credentials, SSH keys)

## Disclosure

Once a fix is available, the vulnerability will be disclosed in a GitHub Security Advisory with credit to the reporter (unless anonymity is requested).
