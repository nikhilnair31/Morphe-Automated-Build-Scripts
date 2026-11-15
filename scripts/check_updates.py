import os, requests, re, subprocess, sys, json

REPO_CLI         = "MorpheApp/morphe-cli"
REPO_MORPHE      = "MorpheApp/morphe-patches"
REPO_DE_REVANCED = "RookieEnough/De-ReVanced"
REPO_DE_VANCED   = "RookieEnough/De-Vanced"
REPO_PIKO        = "crimera/piko"
REPO_HOODLES     = "hoo-dles/morphe-patches"
STATE_FILE       = ".github/last_built_versions.json"

APPS = {
    # ── Morphe native patches ──────────────────────────────────
    "youtube":        {"package": "com.google.android.youtube",               "slug": "youtube",              "source": "morphe"},
    "music":          {"package": "com.google.android.apps.youtube.music",    "slug": "youtube-music",        "source": "morphe"},
    "reddit":         {"package": "com.reddit.frontpage",                     "slug": "reddit",               "source": "morphe"},
    # ── Piko patches ───────────────────────────────────────────
    "twitter":        {"package": "com.twitter.android",                      "slug": "twitter-x",            "source": "piko"},
    "instagram":      {"package": "com.instagram.android",                    "slug": "instagram",            "source": "piko"},
}

def get_latest_release(repo, include_prerelease=False):
    headers = {"Accept": "application/vnd.github.v3+json"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"token {token}"
    if include_prerelease:
        # /releases/latest skips pre-releases; list all and pick newest with a .mpp asset,
        # falling back to the latest stable if no pre-release has the required asset
        url = f"https://api.github.com/repos/{repo}/releases?per_page=20"
        r = requests.get(url, headers=headers)
        r.raise_for_status()
        releases = r.json()
        if not releases:
            raise ValueError(f"No releases found for {repo}")
        pre = next((rel for rel in releases if rel["prerelease"] and any(a["name"].endswith(".mpp") for a in rel["assets"])), None)
        stable = next((rel for rel in releases if not rel["prerelease"] and any(a["name"].endswith(".mpp") for a in rel["assets"])), None)
        chosen = pre if pre else stable
        if not chosen:
            raise ValueError(f"No release with .mpp asset found for {repo}")
        chosen["_used_prerelease"] = chosen["prerelease"]
        return chosen
    url = f"https://api.github.com/repos/{repo}/releases/latest"
    r = requests.get(url, headers=headers)
    r.raise_for_status()
    rel = r.json()
    rel["_used_prerelease"] = False
    return rel

def download_file(url, filename):
    print(f"  Downloading {filename}...")
    headers = {"Accept": "application/octet-stream"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"token {token}"
    with requests.get(url, stream=True, headers=headers, allow_redirects=True) as r:
        r.raise_for_status()
        with open(filename, "wb") as f:
            for chunk in r.iter_content(8192):
                f.write(chunk)

def version_key(v):
    parts = []
    for x in str(v).split("."):
        m = re.match(r"(\d+)", x)
        parts.append(int(m.group(1)) if m else 0)
    return parts

def _is_versioncode_float(v):
    """Return True for de_revanced float versionCodes like 4.314935684204102.
    These are Android versionCodes expressed as floats and are not valid
    APK versionNames — no download source can resolve them."""
    parts = str(v).split(".")
    return len(parts) == 2 and len(parts[1]) >= 9

def get_compatible_versions(cli_jar, mpp_files, package_name):
    cmd = ["java", "-jar", cli_jar, "list-patches"]
    for f in mpp_files:
        cmd.append(f"--patches={f}")
    cmd += ["-f", package_name, "-p", "-v"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except Exception as e:
        print(f"  CLI error for {package_name}: {e}")
        return []
    if result.returncode != 0:
        print(f"  CLI exit {result.returncode} for {package_name}: {result.stderr.strip()[:200]}")
        return []
    all_versions = set()
    cur_pkg = None
    in_versions = False
    for raw in result.stdout.splitlines():
        s = raw.strip()
        if not s:
            in_versions = False
            continue
        if s.startswith("Package name:"):
            cur_pkg = s.split(":", 1)[1].strip()
            in_versions = False
            continue
        if s.startswith("Compatible versions:"):
            in_versions = (cur_pkg == package_name)
            inline = s.split(":", 1)[1].strip()
            if in_versions and inline and inline.lower() not in ("any", "none"):
                for v in inline.split(","):
                    v = v.strip().lstrip("-").strip()
                    if v: all_versions.add(v)
            continue
        if in_versions and re.match(r"^[\d.]", s):
            all_versions.add(s.lstrip("-").strip())
        elif in_versions and ":" in s:
            in_versions = False
    usable = [v for v in all_versions if not _is_versioncode_float(v)]
    if len(usable) < len(all_versions):
        dropped = [v for v in all_versions if _is_versioncode_float(v)]
        print(f"  NOTE: {package_name} — patch bundle reports float versionCode(s) {dropped[:3]}, treating as unconstrained (will use latest store version)")
    return sorted(usable, key=version_key)

def get_apkpure_latest_version(slug, package):
    """Fallback: get latest version when patches have no version constraint.
    Tries Google Play Store first (accessible from CI), then APKPure."""
    headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}

    # 1. Google Play Store — reliably accessible from CI runners
    try:
        url = f"https://play.google.com/store/apps/details?id={package}&hl=en_US&gl=US"
        r = requests.get(url, headers=headers, timeout=20)
        if r.status_code == 200:
            # Play Store embeds version history in the page; grab the highest
            versions = re.findall(r'"(\d+\.\d[\d.]{3,})"', r.text)
            # Exclude float versionCodes (e.g. 4.314935684204102) — Play Store
            # can embed these but they are not resolvable by any APK mirror.
            versions = [v for v in versions if not _is_versioncode_float(v)]
            if versions:
                best = sorted(versions, key=version_key)[-1]
                print(f"  Play Store version: {best}")
                return best
        print(f"  Play Store returned HTTP {r.status_code}")
    except Exception as e:
        print(f"  Play Store scrape failed: {e}")

    # 2. APKPure main page (works from local, may 403 from CI)
    try:
        url = f"https://apkpure.com/{slug}/{package}"
        r = requests.get(url, headers=headers, timeout=20)
        if r.status_code == 200:
            m = re.search(r'"versionName"\s*:\s*"([\d]+\.[\d][\d.]*)"', r.text)
            if m:
                print(f"  APKPure version: {m.group(1)}")
                return m.group(1)
            m = re.search(r'version one-line">([\d]+\.[\d][\d.]*)<', r.text)
            if m:
                print(f"  APKPure version: {m.group(1)}")
                return m.group(1)
        print(f"  APKPure returned HTTP {r.status_code}")
    except Exception as e:
        print(f"  APKPure scrape failed: {e}")

    return ""

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f: return json.load(f)
        except Exception: pass
    return {}

def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f: json.dump(state, f, indent=2)

def set_output(key, value):
    out = os.getenv("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f: f.write(f"{key}={value}\n")
    print(f"  -> {key}={value}")

def main():
    manual_app  = os.environ.get("MANUAL_APP",  "all").strip()
    manual_ver  = os.environ.get("MANUAL_VERSION", "").strip()
    target_arch = os.environ.get("TARGET_ARCH", "arm64-v8a").strip()
    force_all   = os.environ.get("FORCE_ALL", "").strip().lower() in ("1", "true", "yes")

    # ── Fetch all releases ─────────────────────────────────────
    cli_rel     = get_latest_release(REPO_CLI)
    mor_rel     = get_latest_release(REPO_MORPHE)
    drvr_rel    = get_latest_release(REPO_DE_REVANCED)
    dvcd_rel    = get_latest_release(REPO_DE_VANCED)
    piko_rel    = get_latest_release(REPO_PIKO, include_prerelease=True)
    hoodles_rel = get_latest_release(REPO_HOODLES)

    cli_tag     = cli_rel["tag_name"]
    mor_tag     = mor_rel["tag_name"]
    drvr_tag    = drvr_rel["tag_name"]
    dvcd_tag    = dvcd_rel["tag_name"]
    piko_tag    = piko_rel["tag_name"] + (" (pre-release)" if piko_rel.get("_used_prerelease") else "")
    hoodles_tag = hoodles_rel["tag_name"]

    cli_jar     = next((a for a in cli_rel["assets"]     if a["name"].endswith(".jar")), None)
    mor_mpp     = next((a for a in mor_rel["assets"]     if a["name"].endswith(".mpp")), None)
    drvr_mpp    = next((a for a in drvr_rel["assets"]    if a["name"].endswith(".mpp")), None)
    dvcd_mpp    = next((a for a in dvcd_rel["assets"]    if a["name"].endswith(".mpp")), None)
    piko_mpp    = next((a for a in piko_rel["assets"]    if a["name"].endswith(".mpp")), None)
    hoodles_mpp = next((a for a in hoodles_rel["assets"] if a["name"].endswith(".mpp")), None)

    for label, asset in [("CLI jar", cli_jar), ("Morphe mpp", mor_mpp),
                         ("De-ReVanced mpp", drvr_mpp), ("De-Vanced mpp", dvcd_mpp),
                         ("Piko mpp", piko_mpp), ("hoo-dles mpp", hoodles_mpp)]:
        if not asset:
            print(f"ERROR: No asset found for {label}")
            sys.exit(1)

    print(f"Morphe CLI:       {cli_tag}  ({cli_jar['name']})")
    print(f"Morphe patches:   {mor_tag}  ({mor_mpp['name']})")
    print(f"De-ReVanced:      {drvr_tag} ({drvr_mpp['name']})")
    print(f"De-Vanced:        {dvcd_tag} ({dvcd_mpp['name']})")
    print(f"Piko:             {piko_tag} ({piko_mpp['name']})")
    print(f"hoo-dles:         {hoodles_tag} ({hoodles_mpp['name']})")

    for asset in [cli_jar, mor_mpp, drvr_mpp, dvcd_mpp, piko_mpp, hoodles_mpp]:
        if not os.path.exists(asset["name"]):
            download_file(asset["browser_download_url"], asset["name"])

    source_to_mpp = {
        "morphe":      mor_mpp["name"],
        "de_revanced": drvr_mpp["name"],
        "de_vanced":   dvcd_mpp["name"],
        "piko":        piko_mpp["name"],
        "hoodles":     hoodles_mpp["name"]
    }

    # ── Manual build ───────────────────────────────────────────
    if manual_app and manual_app != "all":
        print(f"\nManual build requested: {manual_app} (version_override='{manual_ver}', arch={target_arch})")
        info = APPS.get(manual_app)
        if not info:
            print(f"ERROR: Unknown app '{manual_app}'. Valid keys: {', '.join(APPS)}")
            sys.exit(1)

        if manual_ver:
            version = manual_ver
            print(f"  Using forced version: {version}")
        else:
            mpp_file = source_to_mpp.get(info["source"])
            versions = get_compatible_versions(cli_jar["name"], [mpp_file], info["package"])
            version  = versions[-1] if versions else ""
            if not version:
                print(f"  No pinned versions from CLI (patches work on any version) — falling back to APKPure latest")
                version = get_apkpure_latest_version(info["slug"], info["package"])
            print(f"  Latest supported version: {version or 'NONE'}")

        if not version:
            print(f"ERROR: No compatible version found for {manual_app}")
            sys.exit(1)

        apps_to_build = [{
            "key":     manual_app,
            "name":    manual_app.replace("_", "-"),
            "package": info["package"],
            "slug":    info["slug"],
            "version": version,
            "source":  info["source"],
        }]

        set_output("any_needs_build",  "true")
        set_output("apps_to_build",    json.dumps(apps_to_build))
        set_output("morphe_tag",       mor_tag)
        set_output("de_revanced_tag",  drvr_tag)
        set_output("de_vanced_tag",    dvcd_tag)
        set_output("piko_tag",         piko_tag)
        set_output("hoodles_tag",      hoodles_tag)
        set_output("cli_tag",          cli_tag)
        set_output("morphe_mpp",       mor_mpp["name"])
        set_output("de_revanced_mpp",  drvr_mpp["name"])
        set_output("de_vanced_mpp",    dvcd_mpp["name"])
        set_output("piko_mpp",         piko_mpp["name"])
        set_output("hoodles_mpp",      hoodles_mpp["name"])
        set_output("cli_jar",          cli_jar["name"])
        set_output("target_arch",      target_arch)
        return

    # ── Auto mode ──────────────────────────────────────────────
    last    = load_state()
    current = {
        "cli_tag": cli_tag, "morphe_tag": mor_tag,
        "de_revanced_tag": drvr_tag, "de_vanced_tag": dvcd_tag,
        "piko_tag": piko_tag, "hoodles_tag": hoodles_tag,
    }

    apps_to_build = []
    use_staged = os.environ.get("USE_STAGED", "").strip().lower() in ("1", "true", "yes")
    if use_staged:
        # Bases were staged by Nitro into the "bases" release; build exactly
        # what is staged instead of re-resolving versions here.
        headers = {"Accept": "application/vnd.github.v3+json"}
        if os.environ.get("GITHUB_TOKEN"):
            headers["Authorization"] = f"token {os.environ['GITHUB_TOKEN']}"
        r = requests.get(
            f"https://api.github.com/repos/{os.environ['GITHUB_REPOSITORY']}/releases/tags/bases",
            headers=headers, timeout=30,
        )
        if r.status_code != 200:
            print("  No staging release — nothing to build.")
            set_output("any_needs_build", "false")
            set_output("apps_to_build", "[]")
            set_output("morphe_tag", mor_tag); set_output("de_revanced_tag", drvr_tag)
            set_output("de_vanced_tag", dvcd_tag); set_output("piko_tag", piko_tag)
            set_output("hoodles_tag", hoodles_tag); set_output("cli_tag", cli_tag)
            set_output("morphe_mpp", mor_mpp["name"]); set_output("de_revanced_mpp", drvr_mpp["name"])
            set_output("de_vanced_mpp", dvcd_mpp["name"]); set_output("piko_mpp", piko_mpp["name"])
            set_output("hoodles_mpp", hoodles_mpp["name"]); set_output("cli_jar", cli_jar["name"])
            set_output("target_arch", target_arch)
            return
        by_pkg = {i["package"]: (k, i) for k, i in APPS.items()}
        for asset in r.json().get("assets", []):
            m = re.match(r"(.+?)-v?([0-9][\w.\-]*)\.(apk|xapk|apkm)$", asset["name"])
            if not m:
                continue
            pkg, version = m.group(1), m.group(2)
            hit = by_pkg.get(pkg)
            if not hit:
                print(f"  NOTE: staged asset {asset['name']} has no matching app — skipping")
                continue
            key, info = hit
            apps_to_build.append({
                "key": key, "name": key.replace("_", "-"), "package": pkg,
                "slug": info["slug"], "version": version, "source": info["source"],
            })
            current[key] = version
            print(f"  OK {key} -> {version} (staged)")
        set_output("any_needs_build", "true" if apps_to_build else "false")
        set_output("apps_to_build", json.dumps(apps_to_build))
        set_output("morphe_tag", mor_tag); set_output("de_revanced_tag", drvr_tag)
        set_output("de_vanced_tag", dvcd_tag); set_output("piko_tag", piko_tag)
        set_output("hoodles_tag", hoodles_tag); set_output("cli_tag", cli_tag)
        set_output("morphe_mpp", mor_mpp["name"]); set_output("de_revanced_mpp", drvr_mpp["name"])
        set_output("de_vanced_mpp", dvcd_mpp["name"]); set_output("piko_mpp", piko_mpp["name"])
        set_output("hoodles_mpp", hoodles_mpp["name"]); set_output("cli_jar", cli_jar["name"])
        set_output("target_arch", target_arch)
        return

    for key, info in APPS.items():
        mpp_file = source_to_mpp.get(info["source"])
        versions = get_compatible_versions(cli_jar["name"], [mpp_file], info["package"])
        latest   = versions[-1] if versions else ""
        if not latest:
            latest = get_apkpure_latest_version(info["slug"], info["package"])
        # When no real version can be resolved (patch bundle only has float
        # versionCodes AND Play Store returns nothing usable), queue the app
        # with a sentinel so the build step can download the latest APKPure
        # release without version pinning.
        if not latest:
            latest = "apkpure-latest"
            print(f"  NOTE: {key} — no real version found; queuing as apkpure-latest")
        current[key] = latest

        patch_changed = {
            "morphe":      last.get("morphe_tag")      != mor_tag,
            "de_revanced": last.get("de_revanced_tag") != drvr_tag,
            "de_vanced":   last.get("de_vanced_tag")   != dvcd_tag,
            "piko":        last.get("piko_tag")         != piko_tag,
            "hoodles":     last.get("hoodles_tag")      != hoodles_tag,
        }[info["source"]]

        needs = bool(latest) and (force_all or last.get(key) != latest or patch_changed)
        if needs:
            apps_to_build.append({
                "key":     key,
                "name":    key.replace("_", "-"),
                "package": info["package"],
                "slug":    info["slug"],
                "version": latest,
                "source":  info["source"],
            })
            reason = "forced" if force_all else "rebuild"
            print(f"  OK {key} -> {latest} ({reason})")
        else:
            print(f"  -- {key} -> {latest or 'no versions'}")

    any_needs = len(apps_to_build) > 0
    save_state(current)

    set_output("any_needs_build",  "true" if any_needs else "false")
    set_output("apps_to_build",    json.dumps(apps_to_build))
    set_output("morphe_tag",       mor_tag)
    set_output("de_revanced_tag",  drvr_tag)
    set_output("de_vanced_tag",    dvcd_tag)
    set_output("piko_tag",         piko_tag)
    set_output("hoodles_tag",      hoodles_tag)
    set_output("cli_tag",          cli_tag)
    set_output("morphe_mpp",       mor_mpp["name"])
    set_output("de_revanced_mpp",  drvr_mpp["name"])
    set_output("de_vanced_mpp",    dvcd_mpp["name"])
    set_output("piko_mpp",         piko_mpp["name"])
    set_output("hoodles_mpp",      hoodles_mpp["name"])
    set_output("cli_jar",          cli_jar["name"])
    set_output("target_arch",      target_arch)

    if not os.getenv("SKIP_CLEANUP"):
        for asset in [cli_jar, mor_mpp, drvr_mpp, dvcd_mpp, piko_mpp, hoodles_mpp]:
            if os.path.exists(asset["name"]):
                try:
                    os.remove(asset["name"])
                except Exception as e:
                    print(f"Warning: could not clean up {asset['name']}: {e}")

if __name__ == "__main__":
    main()