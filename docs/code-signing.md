# Code signing

The desktop apps of vinyl-check are built by GitHub Actions from this repository
([`.github/workflows/desktop.yml`](../.github/workflows/desktop.yml)) and attached to each
[release](https://github.com/michaelpietzsch/vinyl-check/releases). This page describes their signing status, what
that means when you first open them, and how signing can be switched on later.

## Current status

The apps are **not signed** with a publisher certificate yet:

- **macOS:** the app carries only an ad-hoc signature and is not notarized by Apple.
- **Windows:** the installer and the program have no Authenticode signature.

Both systems therefore ask once before the first start. That is expected and does not mean that the file is damaged.

### First start on macOS

1. Open the disk image and drag **vinyl-check** to **Applications**.
2. Open vinyl-check. macOS reports that it cannot verify the app. Close the message with **Done**.
3. Open **System Settings → Privacy & Security**. Below *Security* there is a note about vinyl-check: click
   **Open Anyway** and confirm with your password.

From then on, vinyl-check opens normally. Before the first recording, macOS asks for access to the microphone:
this is how macOS names any audio input, including your audio interface. Allow it, otherwise recordings stay silent.
You can change the choice later under *Privacy & Security → Microphone*.

### First start on Windows

When you start the installer, Microsoft Defender SmartScreen may show *Windows protected your PC*. Click
**More info**, check that the app is `vinyl-check-<version>-windows-x64-setup.exe`, then click **Run anyway**.

### Checking a download

GitHub shows a SHA-256 checksum for every file of a release. Compare it with the file you downloaded:

```bash
shasum -a 256 vinyl-check-*-macos-arm64.dmg                         # macOS
```

```powershell
Get-FileHash vinyl-check-*-windows-x64-setup.exe -Algorithm SHA256   # Windows (PowerShell)
```

Every release also carries a `SHA256SUMS` file and a build provenance attestation: with the
[GitHub CLI](https://cli.github.com/), `gh attestation verify <file> --repo michaelpietzsch/vinyl-check` confirms that
the file was built by this repository's workflow from the tagged commit.

## Switching signing on

Signing needs a certificate in the publisher's name and, for most services, a paid plan. The build is prepared so
that signing only needs accounts, secrets and, on macOS, a few workflow steps.

### Windows

Possible services:

- **SignPath** (commercial plan; the free SignPath Foundation program is limited to OSI-approved open-source
  licenses). Already prepared: the signing step in `desktop.yml`, which runs only when the repository secret
  `SIGNPATH_API_TOKEN` exists, and the artifact configuration [`.signpath/artifact-configuration.xml`](../.signpath/artifact-configuration.xml).
- **Microsoft Artifact Signing** (formerly Trusted Signing; monthly subscription with identity validation). Replace
  the SignPath step in `desktop.yml` by the service's GitHub Action, with the installer as the file to sign.
- A classic **OV or EV certificate** from a certificate authority (hardware token or cloud key storage), used with
  `signtool` in the workflow.

Checklist for SignPath:

1. Create the SignPath organization (commercial plan) and turn on multi-factor authentication for the GitHub and
   SignPath accounts of everyone who can release.
2. Install the SignPath GitHub App for the repository (GitHub as trusted build system).
3. In SignPath, create the project `vinyl-check`, add `.signpath/artifact-configuration.xml` as its artifact
   configuration, and create the signing policy `release-signing` with the code signing certificate. Decide whether
   each signing request needs a manual approval (recommended).
4. In the GitHub repository settings, add the secret `SIGNPATH_API_TOKEN` (API token of a SignPath CI user) and the
   variables `SIGNPATH_ORGANIZATION_ID`, and if the slugs differ, `SIGNPATH_PROJECT_SLUG` (default `vinyl-check`)
   and `SIGNPATH_SIGNING_POLICY` (default `release-signing`).
5. Push a version tag. Approve the signing request in SignPath if the policy asks for it; the workflow waits and
   publishes the signed installer.
6. Check the result on a Windows computer: the installer's *Properties → Digital Signatures*.

The installer's version resource names the product `vinyl-check` and the version of the release; the artifact
configuration only accepts a file with exactly these values. Later improvement: sign `vinyl-check.exe` as well,
before the installer is built (a second signing request).

### macOS

Signing and notarization need a membership in the Apple Developer Program and a *Developer ID Application*
certificate. Already prepared: the PyInstaller spec signs with the identity in the environment variable
`VC_CODESIGN_IDENTITY` and passes the entitlements in
[`packaging/macos/entitlements.plist`](../packaging/macos/entitlements.plist) (audio input under the hardened
runtime).

Checklist:

1. Join the Apple Developer Program, create a *Developer ID Application* certificate and export it with its key
   as a `.p12` file.
2. Add repository secrets for the certificate (base64 of the `.p12` and its password) and for notarization (an App
   Store Connect API key, or Apple ID, app-specific password and team ID).
3. Extend the macOS job of `desktop.yml`: import the certificate into a temporary keychain, set
   `VC_CODESIGN_IDENTITY` for the PyInstaller step, sign the disk image, submit it with
   `xcrun notarytool submit --wait` and staple the ticket with `xcrun stapler staple`.
4. Check the result: `spctl --assess --type open --context context:primary-signature -v <dmg>` and a first start on
   a Mac that has not seen the app before (no *Open Anyway* needed).

When the apps are signed, update the section [Current status](#current-status) and the first-start notes in the
README.
