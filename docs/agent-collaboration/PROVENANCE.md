# Collaboration source provenance

Squashed import from Caura collaboration development, reviewed at these source tips:

- Core mounts and security regressions: `814fcd6625ff72177e82a892879981184630e72c`.
- Enterprise gateway and dashboard: `803c609d15bd47a6fc0e42c4e224593d65db9835`.
- Collaboration clients, platform, governance and acceptance: `de3c24d79ee1a5fe886fb77d7f87c985e71824bb`.
- Preceding feature tips: `7869a2a5f141ae2c2b2057aa052d56a92436ec24`, `6af4ff6be2bfb7c843bc68c32521b5a36c7f925c`.

The architect authorized Apache-2.0 for client and wire-model imports, and
Proprietary for the Enterprise platform import, subject to operator review before merge.
The package import names and the existing `mc_` credential contract are preserved.

Native packaging replaces the previous integration wrappers and patch synchronization.
