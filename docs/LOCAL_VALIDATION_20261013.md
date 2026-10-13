# Windows local migration and validation — 2026-10-13

The handoff was imported into a new Windows workspace and verified before any
remote changes were integrated. Original installations, databases, private
settings, APKs, and transfer archives were retained. This report contains no
private key, password, real trip data, or user-specific local path.

## Migration baseline

- Source commit: `34dd51ac7a614bd3df37f932428cf9f8ee61d0c3`.
- The supplied migration ZIP SHA-256 matched the independently supplied value.
- All 184 manifest files matched size and SHA-256 after copying.
- Import receipt, Git bundle completeness, and `git fsck --full --strict` passed.
- Private signing material and real location data remain outside the repository.
- The original handoff is retained unchanged; this document supplements it.

## Fixes verified locally

1. Windows timestamps can repeat between backups. Backup names now include a
   generation and UUID. A directory-scoped operating-system file lock serializes
   generation allocation and retention across processes. Rotation retains the
   newest generations even if timestamps repeat or the clock moves backwards.
   Legacy backup names remain readable and rotate before new-format backups.
2. Java source parsing explicitly uses UTF-8 on Windows, matching the source files.
3. Recovery build output is resolved against the caller's working directory before
   child tools switch directories. An existing output is still refused.
4. Android Bluetooth and location cleanup/startup explicitly handle
   `SecurityException`; existing failure and cleanup behavior is preserved.
   This resolves four `MissingPermission` lint errors without suppressing them.

Cross-process locking, rotation with identical timestamps and reversed UUIDs,
and exception-release regression tests are included. CI now exercises the core
Python suite on Windows and Linux, plus Android host parsing on Windows.

## Local environment and test evidence

Windows 11 ARM64; Python 3.12.14; Git 2.53.0.windows.3;
Microsoft OpenJDK 17.0.20.1 ARM64; Android SDK Platform 35 revision 2;
Build Tools 35.0.0; Gradle 8.11.1; Android Gradle Plugin 8.9.2.
Toolchains are isolated to the migration workspace, with session-only environment
settings. SDK archive checksums and installed archive contents were verified.

| Check | Result |
| --- | --- |
| Python suite | 756 passed, 2 skipped |
| Android host tests | 2,612 assertions; 21 Java source files parsed |
| Recovery Python tests | 14 passed |
| JavaScript tests | 61 passed |
| PowerShell network tests | 57 checks; zero real mutations |
| PowerShell firewall tests | 50 checks; zero real mutations/UAC prompts |
| Backup contention stress | 1,440 backups (12 processes × 10 × 12 rounds), plus 160-backup run passed |
| Android app/instrumentation API compilation | Passed |
| Recovery unsigned app/test APK compilation | Passed |
| Gradle `assembleDebug` | Passed, signing disabled |
| Gradle `lintDebug` | Passed: zero errors, eight warnings |
| Gradle `testDebugUnitTest` | `NO-SOURCE`: **zero** Gradle unit tests executed |
| Relative recovery output and existing-output refusal | Passed; existing output hash unchanged |

The Python skips were an unavailable optional `aiobmsble` dependency and Windows
symlink creation permissions. One upstream Starlette deprecation warning remains.
Lint warnings: DataExtractionRules (1), DrawAllocation (1), GradleOverrides (2),
SetTextI18n (4). SDK-manager deprecation and SDK-metadata compatibility warnings
did not prevent compilation. Platform-tools/ADB and a local emulator were not
installed. Local runtime, physical BLE, and road accuracy tests were not run.

Unsigned validation artifacts are not installation releases:

- Prototype APK SHA-256:
  `c8d1664939a4ed1680664631a6666c52f79d3ef6f19646d90b0831953115b985`.
- Recovery APK SHA-256:
  `47b5013a0cd77799d03b1d7d525d43fd379f4b57a409cea1f2c8a21d93815d2c`.

## Signing and unresolved work

The original signing private key remains **not recovered**, and is the highest
priority unresolved issue. Original package `jp.k4159191.gtbms.prototype` has
certificate SHA-256
`3436a806f9a3f8d1eec53e1a35378db5a5a07d5229aabf32d312ea028af745da`.
The preserved original APK certificates match this fingerprint.

The separately preserved recovery private-key backup was verified, including
private-key presence. Its certificate SHA-256 is
`19aab1ce8543bebd734f05723ad5fdb9eb24806f5fcf15d787aeb32cf2acf9c6`,
for **different** package `jp.k4159191.gtbms.recovery`. It cannot update the
original package. No new signing key was created during local validation.

Read-only local and transferred-record investigations found no matching original
private key. The certificate's validity starts at 2026-10-11 21:51:29 UTC,
equivalent to 2026-10-12 06:51:29 JST. The original build's actual `--keystore`
argument has not been recovered. No evidence establishes key deletion,
revocation, or archive relocation. OpenAI-managed old environments/snapshots,
support inquiries, and Google Play/Cloud certificate matching remain unverified;
they must not be described as completed.

No `START-RECOVERY.cmd`, physical-device ADB operation, uninstall, live-database
replacement, or BMS configuration change was performed. Future physical-device
validation is a separate operation, not part of this PC migration.

Known backup limitations: an interrupted backup may retain its unique `.tmp`
file; POSIX locking waits without a deadline, while Windows waits up to 30 seconds.
Uncoordinated external deletion of backup files is outside the locking protocol.

Future features and requirements remain governed by the preserved
`CODEX-HANDOFF.md` outside the repository. Full raw IMU capture, improved missing-GPS
stop detection, and radio assistance are not implemented by these fixes.
