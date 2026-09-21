# ADR-0013: Deploy by pulling a published image

**Status:** Accepted (implemented; see Action Items) — 2026-09-21
**Date:** 2026-09-21
**Deciders:** Project owner

## Context

The bot runs on a homelab — a guest on a Proxmox host, behind NAT, with no inbound ingress (the same
constraint that chose EventSub over WebSocket in ADR-0001). Until now a deploy meant building the image
on the box: `docker compose up -d --build` from a checkout. That works while the checkout and the running
container are the same machine's business, but it makes every deploy a build, ties what runs to whatever
the working tree happened to contain, and gives nothing to roll back *to*.

CI already builds the image on every push and throws it away. The question is how that image reaches the
guest, and what is allowed to reach into the homelab to put it there.

## Decision

- **CI publishes; the guest pulls.** `main` pushes `ghcr.io/<owner>/doomtp-bot:main` and a
  `:<sha>` tag beside it. Nothing outside the homelab ever connects to the homelab.
- **The guest runs `compose.yaml` plus `compose.prod.yaml`**, which replaces each `build:` with
  `${BOT_IMAGE}` from `.env`. The same repo therefore builds locally for development and pulls in
  production, with no second copy of the service definitions.
- **A systemd timer runs `deploy/update.sh` nightly.** It pulls, does nothing when the digest hasn't
  moved, and otherwise restarts through compose — which sends `SIGTERM` and waits out
  `stop_grace_period`, so a deploy closes the log sessions instead of leaving a gap (ADR-0008). It then
  runs the coverage check, whose exit code is the unit's.
- **The one-shot tools ship in the image.** `scripts/` is copied into `/app/scripts` rather than
  bind-mounted from a checkout, so `coverage`, `backup` and `starter-pack` are the versions that were
  built, not whatever the tree beside them says today.
- **Rolling back is pinning `BOT_IMAGE` to a `:<sha>` tag**, not a rebuild.

## Options Considered

### Option A: build on the box (what it did before)
| Dimension | Assessment |
|-----------|------------|
| Complexity | Low — one command, no registry, no credentials |
| Cost | Free |
| Ingress | None |
| Reproducibility | Poor: the build depends on the tree, and rebuilding an old commit is the only way back |

**Pros:** nothing to set up. The guest needs only git and Docker.
**Cons:** a deploy is a multi-minute build on a 2 vCPU guest, the image is never the one CI tested, and a
rollback means rebuilding a commit rather than starting a known artifact.

### Option B: CI publishes, the guest pulls (chosen)
| Dimension | Assessment |
|-----------|------------|
| Complexity | Medium — a publish step, a registry, one override file and a timer |
| Cost | Free (GHCR for a public package) |
| Ingress | None: the guest opens the connection |
| Reproducibility | Good: the sha tag is exactly what CI tested, and rollback is a tag change |

**Pros:** what runs is what was tested. No credentials on the guest beyond a registry read, which a public
package doesn't even need. The deploy is a pull, so it is fast and interruptible. Rollback is trivial.
**Cons:** the tag is pulled on a timer, so a deploy lands within a day rather than on push (running the
unit by hand is the "now" button). Old images accumulate until pruned.

### Option C: a self-hosted GitHub Actions runner in the guest
| Dimension | Assessment |
|-----------|------------|
| Complexity | Medium-high — a runner to install, update and keep alive |
| Cost | Free |
| Ingress | None: the runner polls |
| Reproducibility | Good |

**Pros:** deploys land on push, with the workflow log as the deploy log.
**Cons:** the runner needs the Docker socket, which is root on the guest, and anything that can trigger a
workflow can then run code there. Fork pull requests must never reach it. That is a lot of care to buy a
few hours of latency.

### Option D: SSH from GitHub Actions
| Dimension | Assessment |
|-----------|------------|
| Complexity | Medium |
| Cost | Free, plus whatever provides the path in |
| Ingress | **Required** — a forwarded port or an overlay network |
| Reproducibility | Good |

**Pros:** immediate, and the deploy logic lives with the workflow.
**Cons:** it needs a way into the homelab and a key in GitHub that opens it. Both are exactly what this
project has avoided everywhere else.

## Trade-off Analysis

B beats A on the thing that matters after an incident: being able to say which image is running and start
the previous one. B beats C and D on attack surface — the guest makes outbound connections only, and the
worst a stolen registry credential buys is a read of a public image. The cost is deploy latency, which is
the cheapest thing on the list to give up for a hobby bot, and the timer is a `systemctl start` away from
being immediate.

## Consequences

- **Easier:** knowing what is deployed, rolling back, and deploying at all (a pull, not a build).
- **Harder:** the guest needs `BOT_IMAGE` set and the two compose files, and iterating on `scripts/`
  locally now needs `--build` because they are baked into the image rather than mounted over it.
- **Sharp edge:** **migrations are forward-only and run at startup.** Rolling back to an image from before
  a migration will meet a schema it doesn't know. Roll back to a sha from the same schema, or restore a
  backup taken before the deploy.
- **Operational:** the image is only as fresh as its base, so a monthly rebuild of `main` is worth having
  even when nothing changed. Old images are pruned by the update script when they stop being referenced.
- **Revisit:** if deploys ever need to land on push, Option C behind a dedicated unprivileged runner is
  the next step, not Option D.

## Action Items

1. [x] Copy `scripts/` into the image and drop the `./scripts` bind mounts from compose. *(2026-09-21)*
2. [x] Publish `:main` and `:<sha>` to GHCR from the `docker` job on pushes to `main`. *(2026-09-21)*
3. [x] `compose.prod.yaml`, `deploy/update.sh` and the systemd timer, with the README section that installs
   them. *(2026-09-21)*
4. [ ] Run it for real on the Proxmox guest: `BOT_IMAGE` set, first pull, first timed update. Needs a git
   remote and a GHCR package to exist, neither of which does yet.
5. [ ] Raise the guest's shutdown timeout (`DefaultTimeoutStopSec` and the VM's own) past the 45 s grace
   period, and install `qemu-guest-agent`, so a host reboot isn't recorded as an unclean shutdown.
