# Eneru Package Repository

Official APT and YUM/DNF package repository for [Eneru](https://github.com/m4r1k/Eneru).

## Stable Releases

### Debian / Ubuntu

```bash
# Import GPG key
curl -fsSL https://m4r1k.github.io/Eneru/KEY.gpg | sudo gpg --dearmor -o /usr/share/keyrings/eneru.gpg

# Add repository
echo "deb [arch=all signed-by=/usr/share/keyrings/eneru.gpg] https://m4r1k.github.io/Eneru/deb stable main" | sudo tee /etc/apt/sources.list.d/eneru.list

# Install
sudo apt update
sudo apt install eneru
```

### RHEL / Fedora

```bash
# RHEL 8/9: Enable EPEL first (required for apprise dependency)
sudo dnf install -y epel-release

# Add repository
sudo curl -o /etc/yum.repos.d/eneru.repo https://m4r1k.github.io/Eneru/rpm/eneru.repo

# Install
sudo dnf install eneru
```

## Testing / Pre-release

> **Note:** The testing channel contains release candidates (rc), beta, and alpha versions.
> These may be unstable. Use in production at your own risk.

### Debian / Ubuntu

```bash
# Add testing repository (GPG key must already be imported)
echo "deb [arch=all signed-by=/usr/share/keyrings/eneru.gpg] https://m4r1k.github.io/Eneru/deb testing main" | sudo tee /etc/apt/sources.list.d/eneru-testing.list

sudo apt update
sudo apt install eneru
```

### RHEL / Fedora

```bash
sudo curl -o /etc/yum.repos.d/eneru-testing.repo https://m4r1k.github.io/Eneru/rpm/testing/eneru-testing.repo

sudo dnf install eneru
```

## Direct Downloads

Download packages directly from [GitHub Releases](https://github.com/m4r1k/Eneru/releases).

## GPG Verification

All repository metadata is signed. Import the [GPG key](https://m4r1k.github.io/Eneru/KEY.gpg) to verify packages.
