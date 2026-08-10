# AWS Client VPN Remote Access — Troubleshooting Reference

**Date:** 2026-07-16
**Scope:** Getting reliable remote access (EKS API from WSL CLI tools + web apps from a Windows browser) working over a single AWS Client VPN connection, from arbitrary locations (not just UW-Madison campus network).
**Related:** [ADR 014](../decisions/014-cloudflare-tunnel-over-vpn.md) (planned long-term replacement — Cloudflare Tunnel/Access removes the VPN-routing dependency entirely; everything below is a stopgap until that lands). `handoffs/vpn-cloudflare-access-migration.md` Thread 1 is the session that introduced the Client-VPN-CIDR security group trust this doc builds on.

## Summary

Getting `argo.<YOUR_DOMAIN>` (and Grafana/ArgoCD) reachable from a Windows 11 browser, concurrently with WSL-side CLI work (`argo submit`, `terraform apply`), while working fully remote (no UW-Madison network access at all) required seven independent fixes. Each layer looked fine in isolation; the failure only showed up when actually testing end-to-end. **If access breaks again, don't assume it's one thing — walk the checklist in §3.**

Current verified-working state (2026-07-16, after §1.7): `ping`/general internet works in WSL while VPN is connected, reliably across repeated reconnects; web apps reachable in Firefox via FoxyProxy → WSL SOCKS5 proxy → VPN tunnel; EKS API reachable for `kubectl`/`terraform`/`argo` CLI commands.

> **§1.7 is the one that actually mattered.** Fixes 1.1–1.6 were all real, but the residual "sometimes it works, sometimes it doesn't" behaviour they left behind was **not** a client-side race (as §1.5 originally claimed) — it was a Client VPN route table that only covered one of three associated subnets. Read §1.7 first.

## 1. The seven fixes, in the order we found them

### 1.1 — Split-tunnel never routed web-app traffic through the VPN at all

**Symptom:** VPN connects fine, EKS API works, but browser/curl to the web apps times out.
**Root cause:** `split_tunnel = true` (the old default, [terraform/variables.tf](../../terraform/variables.tf)) only routes the VPN's authorized CIDRs (the private VPC CIDRs) through the tunnel. The web apps are **public** ALBs — traffic to their public IPs never entered the tunnel at all, so the Client-VPN-CIDR security group trust (added in the handoff's Thread 1) never had a chance to apply. Confirmed via `ip route get <alb-ip>` showing `eth0`, not `tun0`.
**Fix:** `split_tunnel` default flipped to `false` (full tunnel).

### 1.2 — Full tunnel needs an explicit internet route + authorization rule

**Symptom:** After 1.1, turning the VPN on killed *all* internet access, including `ping 8.8.8.8`.
**Root cause:** AWS Client VPN does not automatically add a `0.0.0.0/0` route or authorization rule just because `split_tunnel = false` — both must be created explicitly, or full-tunnel clients get redirected into the tunnel with nowhere to go.
**Fix:** Added `aws_ec2_client_vpn_authorization_rule.internet` and `aws_ec2_client_vpn_route.internet` (both `0.0.0.0/0`, routed via a private subnet with NAT gateway egress) in [terraform/vpn.tf](../../terraform/vpn.tf).

### 1.3 — NAT gateway hairpin defeats the Client-VPN-CIDR security group trust

**Symptom:** After 1.1 and 1.2, general internet worked (`ping` succeeded through the tunnel) but the web apps *still* timed out.
**Root cause:** Traffic from a full-tunnel client to a **public** ALB IP takes this path: client → tunnel → VPC → NAT Gateway → Internet Gateway → internet → back in through the ALB's public IP. By the time it reaches the ALB, the security group sees the **NAT Gateway's Elastic IP** as the source — not the VPN client CIDR (`10.0.0.0/16`), which only applies to traffic that stays VPC-internal. The existing "trust the Client VPN CIDR" rule from Thread 1 (handoff) never covers this hairpinned path.
**Fix:** Added a second trust rule — the NAT Gateway's EIP (`module.vpc.nat_public_ips[0]`) — to all three web-app ALB security groups: `grafana_lb_https_nat` / `grafana_lb_http_nat` ([terraform/grafana.tf](../../terraform/grafana.tf)), `argocd_lb_http_nat` ([terraform/argocd.tf](../../terraform/argocd.tf) — HTTPS already had this from an earlier, unrelated Dex-SSO fix), and `lb_https_nat` in the Argo Workflows module ([terraform/modules/argo-workflows/main.tf](../../terraform/modules/argo-workflows/main.tf), wired via a new `nat_gateway_ip` module variable).

**Aside — Argo Workflows has no port-80 rule at all**, unlike Grafana/ArgoCD (which have both 80 and 443, with the ALB redirecting 80→443). Always use `https://argo.<YOUR_DOMAIN>`, never `http://` — it will time out regardless of anything else being correct.

### 1.4 — VPN endpoint's own ingress was restricted to the UW-Madison prefix list

**Symptom:** Worked fine from home; from a coffee shop (or anywhere off-campus), the VPN wouldn't even connect — not a routing problem, the UDP/443 handshake itself never got through.
**Root cause:** [terraform/vpn.tf](../../terraform/vpn.tf)'s `aws_security_group.vpn` ingress only allowed UDP/443 from `data.aws_ec2_managed_prefix_list.uw_madison` — i.e. you had to already be on (or tunneled into) the UW-Madison campus network just to *reach* the AWS Client VPN endpoint. The operator works remotely full-time and the UW-Madison VPN can't run concurrently with the AWS Client VPN (competing tun adapters/DNS push — see the handoff's Thread 1 troubleshooting note), so this was an unresolvable catch-22 for anyone off-campus.
**Fix:** Opened that ingress rule to `0.0.0.0/0`. Mutual-TLS client certificate authentication (`authentication_options` on the endpoint) is the real access control now, not source IP.

**⚠️ Self-inflicted incident during this fix:** a failed `-refresh=false` apply attempt (revoke succeeded, authorize step errored) briefly left this security group as "allow all protocols, all ports, from anywhere" before being caught (via a follow-up `terraform apply` + direct `aws ec2 describe-security-groups` verification) and corrected within minutes. Root cause: `aws_security_group`'s inline-rules resource type reconciles the whole rule list via separate non-atomic revoke/authorize API calls; a failure partway through can leave an inconsistent intermediate state. **Recommended follow-up (not yet done):** migrate `aws_security_group.vpn` to the `aws_vpc_security_group_ingress_rule`/`_egress_rule` resources already used for the three web-app SGs, which change one rule per resource atomically and don't have this failure mode.

### 1.5 — Intermittent OpenVPN-on-WSL2 route-install race

> **⚠️ Partially misdiagnosed — see §1.7.** The WSL2 race described here is real and the `--route-up` fix is worth keeping, but it was **not** the cause of the intermittent "connects but no internet" behaviour. That was §1.7 (Client VPN routes covering only one of three associated subnets). The route-up script made the client side deterministic, which masked a server-side coin flip and sent this investigation chasing the wrong layer for hours. Treat this section as a hardening measure, not an explanation of intermittency.

**Symptom:** Unpredictable — sometimes a fresh connect gets full-tunnel routing working immediately, sometimes `ping`/browsing fails with no visible error, even though the AWS side is unchanged and confirmed correct via `terraform plan`.
**Root cause:** The server correctly pushes `redirect-gateway def1 bypass-dhcp` every time (verify with `sudo grep -i push /tmp/openvpn.log`), but OpenVPN's *automatic* installation of the resulting `0.0.0.0/1` / `128.0.0.0/1` routes sometimes fails silently on WSL2 — logs show `net_route_v4_add: 0.0.0.0/1 via ... dev [NULL]` instead of a resolved device. When it fails, traffic stays on the pre-existing `eth0` default route instead of the tunnel.
**Fix:** `~/.local/bin/vpn-connect.sh` now passes `--script-security 2 --route-up ~/.local/bin/vpn-route-up.sh`. The route-up script runs after every connect and does `ip route replace 0.0.0.0/1 via $route_vpn_gateway dev $dev` (and the `128.0.0.0/1` counterpart) — idempotent, so it's a no-op if OpenVPN's own install already succeeded, and a fix if it didn't.

**Note on the `/1`+`/1` split:** this is intentional (`redirect-gateway def1`), not a bug — splitting `0.0.0.0/0` into two `/1` routes lets them win via longest-prefix-match over the original default route without deleting it, so the tunnel's own transport traffic (to reach the real VPN server) can still use the original gateway instead of routing into itself.

### 1.6 — Stale/duplicate VPN connections from repeated launches

**Symptom:** Multiple `tun0`–`tun3` interfaces simultaneously active, with duplicate/conflicting routes to the same CIDRs.
**Root cause:** `~/.local/bin/vpn-up` opens a **new** Windows Terminal tab and runs `vpn-connect.sh` every time it's invoked — it doesn't check for an existing connection first. Repeated invocations (stray double-click, running it out of habit) each launch their own `openvpn` process without killing the previous one.
**Fix:** `vpn-connect.sh` now kills any existing `openvpn --config ...` process (`pkill -f "openvpn --config"`) before starting a new one — regardless of how many times `vpn-up` gets triggered, at most one connection survives.

### 1.7 — Internet route existed for only one of three associated subnets (the real cause of the intermittency)

**Symptom:** VPN connects, `tun0` is up with a valid client IP, both `/1` full-tunnel routes are installed, `ip route get 8.8.8.8` correctly returns `dev tun0` — and yet `ping 8.8.8.8` is 100% loss and `curl https://1.1.1.1` (raw IP, no DNS) times out. Every AWS-side config check passes and `terraform plan` reports no drift.

**Root cause:** `aws_ec2_client_vpn_route.internet` targeted `tolist(module.vpc.private_subnets)[0]` — **one** subnet. A Client VPN route is scoped to a single target subnet, but the endpoint has **three** subnet associations, and a connecting client is assigned to one of them unpredictably (it draws its IP from that subnet's /27 slice of `client_cidr_block`). Land on subnet `[0]` → internet works. Land on either of the other two → traffic enters the tunnel, reaches AWS, matches no route, and is silently dropped. **A ~2-in-3 chance of "connects but no internet" on every connect.** `aws_ec2_client_vpn_route.secondary_cidrs` had the identical `[0]` bug for `10.1.0.0/16` / `10.2.0.0/16`.

The `10.0.0.0/16` routes never showed this problem because AWS **auto-creates** those for every association (`Origin: associate`). Only routes you declare yourself (`Origin: add-route`) had the gap — which is why VPC-internal access felt more reliable than internet access.

**Fix:** `for_each` over all private subnets for both resources, in [terraform/vpn.tf](../../terraform/vpn.tf). The route table should be a complete grid — **every destination × every associated subnet** (verify with the command in §3, step 4). Verified by connecting/reconnecting repeatedly: internet works every time, where previously it was roughly one in three.

**Terraform gotcha when applying this:** adding `for_each` changes each resource's address, so Terraform destroys the old resource and creates the new one — with no dependency between them, **the order is arbitrary**. Where it created before destroying, AWS rejected the duplicate with `InvalidClientVpnDuplicateRoute: Route already exists`. The old routes got destroyed anyway, so simply **re-running the same apply succeeds**. Don't panic and start hand-deleting routes.

**How this was actually found (the transferable part):** every static config check passed — endpoint, security groups, NACLs, NAT gateway health and metrics, certificates, client-side routes — and `terraform plan` said "No changes". The config was simultaneously drift-free *and* wrong, because the bug lived in the **relationship** between two individually-correct things (three associations, one route). What cracked it was **per-session byte counters** from `aws ec2 describe-client-vpn-connections`:

```bash
aws ec2 describe-client-vpn-connections --region <YOUR_AWS_REGION> \
  --client-vpn-endpoint-id cvpn-endpoint-0c6c1dede339c10dd \
  --query 'Connections[].{IP:ClientIp,Start:ConnectionEstablishedTime,In:IngressBytes,Out:EgressBytes}' --output table
```

Sessions whose client IP fell in one particular /27 moved gigabytes; every session in a different /27 showed ~100 KB ingress and ~5 KB egress — i.e. **the client's traffic reached AWS and nothing came back**. That asymmetry localised the fault to the AWS-side return path in minutes, after hours of config inspection had found nothing.

**Lesson: when every config check passes but traffic still dies, stop reading config and go measure the data path.** Byte counters, not YAML.

### 1.8 — The NAT-hairpin fix (§1.3) missed two ALBs: Prefect and Kubecost (found 2026-07-17)

**Symptom:** Grafana, ArgoCD, and Argo Workflows all became reachable over the full-tunnel VPN after §1.3's fix, but Prefect (`prefect.<YOUR_DOMAIN>`) and Kubecost (`kubecost.<YOUR_DOMAIN>`) still timed out.

**Root cause:** §1.3's NAT-gateway-EIP trust rule was applied to Grafana, ArgoCD, and Argo Workflows' ALB security groups, but never to Prefect's or Kubecost's — they were simply missed in that pass, not excluded for any reason. Kubecost's gap was wider than Prefect's: its security group ([terraform/modules/finops/kubecost.tf](../../terraform/modules/finops/kubecost.tf)) had *only* the single UW-Madison-prefix-list HTTPS rule — it never got the port-80 prefix-list rule, the Client-VPN-CIDR rules, or the NAT rules that Grafana/ArgoCD have (1 rule vs. their 6).

**Fix:**
- [terraform/modules/prefect/main.tf](../../terraform/modules/prefect/main.tf) — added `lb_https_nat` (443 from NAT EIP), a new `nat_gateway_ip` module variable, wired from [terraform/prefect.tf](../../terraform/prefect.tf). ~~Prefect's ALB has no port-80 listener (oauth2-proxy, HTTPS only), so no HTTP-side rule was needed.~~ **Wrong — corrected 2026-07-17, see §1.9.** The ingress's `listen-ports` annotation *does* configure an HTTP:80 listener (whose only job is the 301→HTTPS redirect), so it needs port-80 SG rules just like the others.
- [terraform/modules/finops/kubecost.tf](../../terraform/modules/finops/kubecost.tf) — brought to full parity with Grafana/ArgoCD: added `kubecost_lb_http` (prefix-list, 80), `kubecost_lb_https_client_vpn` / `kubecost_lb_http_client_vpn` (VPC CIDR), and `kubecost_lb_https_nat` / `kubecost_lb_http_nat` (NAT EIP). New `vpc_cidr` and `nat_gateway_ip` module variables, wired from [terraform/finops.tf](../../terraform/finops.tf).

**Applied 2026-07-17.** The fix above was *written* on 2026-07-16 but **not applied** — `terraform apply` was never run, so the Prefect/Kubecost ALB security groups still lacked the NAT rule the next day. Prefect stayed unreachable until a scoped `terraform apply -target=module.prefect -target=module.finops` (1 add, 3 change, 0 destroy) actually created `module.prefect.aws_vpc_security_group_ingress_rule.lb_https_nat` (NAT EIP `18.189.149.176/32`); connectivity returned immediately (HTTP 200). **A committed Terraform change is intent, not state — it changes nothing in AWS until applied.** Note the full unscoped plan also wanted to touch `module.metrics.aws_glue_catalog_table.{costs,step_outcomes,subject_manifests}` (the drift called out in §5); the `-target` scope was used specifically to avoid dragging that unrelated, production-cost-table change into a VPN fix.

**Lesson: this pattern (public ALB behind the UW-Madison prefix list + full-tunnel VPN) now applies to five apps — Grafana, ArgoCD, Argo Workflows, Prefect, Kubecost.** If a sixth one shows up, or a new web app is added, check it against all 6 rules in §3 step 4's spirit (prefix-list × 2 ports, client-VPN-CIDR × 2 ports, NAT-EIP × 2 ports) rather than assuming the fix from this doc was applied universally — it wasn't, twice.

### 1.9 — Prefect ALB didn't redirect HTTP→HTTPS (missing port-80 SG rules) (found 2026-07-17)

**Symptom:** Prefect was reachable over HTTPS (after §1.8), but `http://prefect.<YOUR_DOMAIN>` did not redirect to HTTPS — it just hung. Every other app's ALB redirects.

**Root cause:** The ALB was configured correctly — its HTTP:80 listener's default action is a proper `HTTP_301` redirect to 443 (from the ingress's `ssl-redirect: "443"` annotation). But the ALB **security group had no port-80 ingress rule at all** (only the two 443 rules from §1.8). Inbound HTTP was dropped at the SG before it could reach the redirect listener, so `http://` timed out instead of redirecting. This is the direct consequence of §1.8's incorrect "no port-80 listener" assumption: because we believed there was no HTTP listener, we never opened port 80 — but the listener existed the whole time, just unreachable.

**Fix:** [terraform/modules/prefect/main.tf](../../terraform/modules/prefect/main.tf) — added `lb_http` (80, prefix-list) and `lb_http_nat` (80, NAT EIP), mirroring the two existing 443 rules onto port 80. Applied via `terraform apply -target=module.prefect` (2 add, 0 change, 0 destroy). Verified: `curl -sI http://prefect...` now returns `301 Moved Permanently → Location: https://…`.

**Kubecost checked, already correct:** its ALB has the port-80 redirect listener *and* the port-80 SG rules (NAT EIP + VPC CIDR + prefix-list), so it redirects fine — no change needed.

**Parity gap closed 2026-07-17.** Prefect had been missing the **client-VPN-CIDR (`10.0.0.0/16`) rules** that Kubecost/Grafana/ArgoCD have — it only had prefix-list + NAT-EIP. Added `lb_https_client_vpn` / `lb_http_client_vpn` (VPC CIDR on 443 / 80) using the already-wired `vpc_cidr` variable; applied via `terraform apply -target=module.prefect` (2 add, 0 change, 0 destroy). Prefect's ALB SG now has the full canonical **6 rules** (prefix-list, VPC-CIDR, NAT-EIP × ports 80/443), matching the other four apps — so operator-workstation, full-tunnel (NAT hairpin), and split-tunnel/VPC-internal (VPC-CIDR SNAT) paths all work on both ports. **All five app ALBs are now at full parity.**

## 2. Windows browser access (separate from the VPN itself)

Windows and WSL2 are separate network stacks by default (NAT mode) — a VPN connected inside WSL doesn't automatically become reachable from Windows apps. **Do not** try to fix this with WSL2 "mirrored networking mode" (`.wslconfig`'s `networkingMode=mirrored`) — it mirrors *Windows'* interfaces into Linux, the wrong direction for this problem, and Microsoft's own docs flag OpenVPN as having known compatibility issues with mirrored mode. This was checked against current docs and deliberately not used.

**What actually works:** a SOCKS5 proxy (`microsocks`) running inside WSL as a systemd service, bound to `127.0.0.1:1080`. WSL2's default `localhostForwarding` (on by default, no config needed) lets Windows reach it via plain `127.0.0.1:1080`. Point Firefox at it via the FoxyProxy extension.

**FoxyProxy gotcha:** adding the proxy definition (SOCKS5, `127.0.0.1`, `1080`) is not enough — you must *also* explicitly select a mode (either "use this proxy for all URLs," or a pattern matching the target hostname). It's easy to define the proxy and never activate it, silently falling back to direct connections that the ALB security groups reject.

**Quick test to check if FoxyProxy is actually engaged:** visit `https://icanhazip.com` — it should return the NAT Gateway's EIP (see §4), not your real ISP IP.

## 3. Troubleshooting checklist (walk in order)

1. **Is the VPN actually connected with full-tunnel routes?**
   ```bash
   ip addr show tun0        # exists?
   ip route show | grep "0.0.0.0/1\|128.0.0.0/1"   # both present?
   ```
   If `tun0` is up but the `/1` routes are missing, the route-up script (§1.5) should have caught this automatically if you connected via `vpn-connect.sh`. If you connected some other way, reconnect via `~/.local/bin/vpn-connect.sh`.

   **If `tun0` is up and the `/1` routes ARE present but there's still no internet, skip ahead to step 4 — that's the §1.7 signature, not a client-side problem.** `vpn-connect.sh` passes `--log /tmp/openvpn.log --verb 4`, so `sudo rg -i "push|Data Channel|Initialization" /tmp/openvpn.log` shows what the server actually pushed and whether the data channel came up.

2. **Multiple stale connections?**
   ```bash
   ps aux | grep "openvpn --config"
   ```
   More than one set of processes means duplicate/conflicting tunnels. `vpn-connect.sh` now prevents this going forward, but if you bypassed it: `sudo pkill -f "openvpn --config"`, reconnect via the script.

3. **Has the AWS-side config drifted?** Run from an environment with normal internet (VPN doesn't matter for this — it's all plain AWS API calls):
   ```bash
   cd terraform
   terraform plan -target=aws_security_group.vpn \
     -target=aws_security_group.grafana_lb -target=aws_security_group.argocd_lb \
     -target=module.argo_workflows.aws_security_group.lb \
     -target='aws_ec2_client_vpn_authorization_rule.vpn' \
     -target='aws_ec2_client_vpn_authorization_rule.internet' \
     -target='aws_ec2_client_vpn_route.internet'
   ```
   ⚠️ **"No changes" does NOT mean the AWS side is fine** — that was the trap in §1.7. The config was drift-free and still wrong, because the defect was a missing *combination* (routes × subnets), not a changed value. Terraform can only tell you AWS matches your config; it cannot tell you your config is complete. Always also run step 4.

   If a full unscoped `terraform plan` is needed instead, note that it will error on every `kubernetes`/`helm`/`kubectl`-backed resource unless run from something with EKS API connectivity (VPN connected) — the errors look like `dial tcp <ip>:443: connect: no route to host` and are expected/harmless to the AWS-only resources' diff.

4. **Is the Client VPN route table a complete grid?** (§1.7 — check this whenever internet fails but the tunnel is up)
   ```bash
   aws ec2 describe-client-vpn-routes --region <YOUR_AWS_REGION> \
     --client-vpn-endpoint-id cvpn-endpoint-0c6c1dede339c10dd \
     --query 'sort_by(Routes,&DestinationCidr)[].{Dest:DestinationCidr,Target:TargetSubnet,Origin:Origin,State:Status.Code}' --output table
   ```
   Expect **every destination × every associated subnet** — currently 4 destinations (`0.0.0.0/0`, `10.0.0.0/16`, `10.1.0.0/16`, `10.2.0.0/16`) × 3 subnets = **12 active routes**. Any destination present for fewer than all three subnets means intermittent failures that depend on which subnet a connect lands on. Cross-check the association list with `aws ec2 describe-client-vpn-target-networks`.

5. **Which subnet did the failing session land on, and did traffic come back?** (the §1.7 diagnostic)
   ```bash
   aws ec2 describe-client-vpn-connections --region <YOUR_AWS_REGION> \
     --client-vpn-endpoint-id cvpn-endpoint-0c6c1dede339c10dd \
     --query 'Connections[].{IP:ClientIp,Start:ConnectionEstablishedTime,In:IngressBytes,Out:EgressBytes}' --output table
   ```
   High `In` with near-zero `Out` = your traffic reached AWS and nothing returned → AWS-side routing (go to step 4). Near-zero both = traffic never left the client → client-side (steps 1–2). Note CloudWatch connection logs publish on a ~30-minute delay, but `describe-client-vpn-connections` is live.

6. **Is FoxyProxy actually engaged?** `https://icanhazip.com` should return the NAT Gateway's EIP, not your ISP's IP. If not, check FoxyProxy's mode (§2), not the VPN.

7. **Using the right protocol?** `https://argo.<YOUR_DOMAIN>` — never `http://` (§1.3 aside).

8. **Claude Code (or this session) unresponsive while the VPN is on?** Observed but not fully root-caused: turning the user's WSL VPN on has, at least once, coincided with Claude Code's own connection to Anthropic's API becoming unreachable, resolved by turning the VPN back off. Mechanism unclear (possibly the NAT-hairpin path being too unstable for a persistent connection) — if this recurs, turn the VPN off to restore communication rather than waiting.

## 4. Reference values (as of 2026-07-16 — verify before trusting if this is stale)

| What | Value |
|---|---|
| Client VPN endpoint ID | `cvpn-endpoint-0c6c1dede339c10dd` |
| VPN endpoint's own security group | `sg-03e235aab34f0b15c` |
| NAT Gateway EIP (what ALBs see for hairpinned full-tunnel traffic) | `18.189.149.176` |
| VPC CIDR (what ALBs see for genuinely VPC-internal VPN traffic) | `10.0.0.0/16` |
| `vpn-connect.sh` / `vpn-route-up.sh` | `~/.local/bin/` (WSL) |
| Generated client config | `terraform/client.ovpn` (regenerated by `local_file.vpn_config` in `terraform/vpn.tf`) |

## 5. Known related-but-separate issue (not fixed here)

While running full `terraform plan`s during this session, found real (non-cosmetic) drift in `module.metrics.aws_glue_catalog_table.{costs,step_outcomes,subject_manifests}` — column order in Terraform's config doesn't match what a Glue Crawler discovered live (a circular shift, not just crawler metadata noise). Unrelated to VPN/access; deliberately left untouched since it affects production Athena/Kubecost cost-reporting tables and deserves its own investigation before any apply.
