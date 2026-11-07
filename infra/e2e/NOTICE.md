`compose.upstream.yaml` is an unmodified copy of SandboxAQ's Agent Switch
standalone deployment at commit `7e57cd433a922bc91a00150c106e6ef1f15b1057`
(`switch-v0.31.0`). Its upstream license, including the modified Commons Clause,
is preserved in `LICENSE.switch`.

Source: https://github.com/sandbox-quantum/switch/blob/7e57cd433a922bc91a00150c106e6ef1f15b1057/deploy/local/standalone-docker-compose.yml

`compose.e2e.yaml` is FindBack's isolation override: immutable image digests,
random loopback ports, no exposed database, resource ownership labels, disabled
telemetry/avatar services, and no host credential mounts. The four OCI index
digests were resolved from their official registries on November8,2026 using
`docker buildx imagetools inspect`. PostgreSQL's pinned index reports16.15-alpine3.24.

Reference reviewed, without copying application code: Overruled's clean-install
E2E workflow at commit `200c869afb01b3194b8504900b59e63fbbc8157a`:
https://github.com/Kevrc/overruled/blob/200c869afb01b3194b8504900b59e63fbbc8157a/.github/workflows/e2e.yml
