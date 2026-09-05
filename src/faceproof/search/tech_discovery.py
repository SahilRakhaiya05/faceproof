from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from .base import SearchCandidate, is_social_profile_url, normalize_page_url, platform_name


@dataclass(frozen=True, slots=True)
class TechProfile:
    platform: str
    url: str
    title: str
    avatar_url: str | None = None
    bio: str | None = None
    handle: str | None = None
    name: str | None = None
    verified: bool = True

    def to_candidate(self, rank: int = 1) -> SearchCandidate:
        norm_url = normalize_page_url(self.url)
        return SearchCandidate(
            provider="tech-discovery",
            rank=rank,
            page_url=self.url,
            normalized_url=norm_url,
            title=self.title,
            source=self.platform.capitalize(),
            image_url=self.avatar_url,
            thumbnail_url=self.avatar_url,
            provider_score=1.0,
            exact_match=False,
            result_type="verified_developer_profile",
            post_id=None,
        )


def extract_identity_seeds(
    candidates: list[SearchCandidate] | tuple[SearchCandidate, ...] | list[dict[str, Any]],
    web_labels: tuple[str, ...] | list[str] = (),
) -> tuple[set[str], set[str]]:
    """Extract handles and personal names from search candidates, labels, and local git."""
    handles: set[str] = set()
    names: set[str] = set()

    for cand in candidates:
        url = cand.normalized_url if hasattr(cand, "normalized_url") else cand.get("url", "")
        title = cand.title if hasattr(cand, "title") else cand.get("title", "")

        # URL extraction
        for m in re.finditer(r"github\.com/([a-zA-Z0-9_-]+)", url, re.I):
            handle = m.group(1)
            if handle.lower() not in {"topics", "trending", "explore", "settings", "about"}:
                handles.add(handle)

        for m in re.finditer(r"devfolio\.co/@([a-zA-Z0-9_-]+)", url, re.I):
            handles.add(m.group(1))

        for m in re.finditer(r"huggingface\.co/([a-zA-Z0-9_-]+)", url, re.I):
            h = m.group(1)
            if h.lower() not in {"models", "datasets", "spaces", "docs", "blog", "pricing"}:
                handles.add(h)

        for m in re.finditer(r"linkedin\.com/in/([a-zA-Z0-9_-]+)", url, re.I):
            handles.add(m.group(1))

        for m in re.finditer(r"(?:x\.com|twitter\.com)/([a-zA-Z0-9_-]+)", url, re.I):
            h = m.group(1)
            if h.lower() not in {"home", "explore", "intent", "share", "search"}:
                handles.add(h)

        # Title patterns
        if title:
            m = re.search(r"^([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b", title)
            if m:
                cand_name = m.group(1).strip()
                if not any(
                    word in cand_name.lower()
                    for word in ("gentleman", "outerwear", "suit", "jacket", "formal", "clothing")
                ):
                    names.add(cand_name)

    # Web labels
    for label in web_labels:
        clean = label.strip()
        if re.match(r"^[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+$", clean) and not any(
            w in clean.lower()
            for w in ("outerwear", "suit", "clothing", "formal", "beard", "model")
        ):
            names.add(clean)

    # Normalized handle variants: preserve authentic handles without truncating
    expanded_handles: set[str] = set()
    generic_noise = {
        "admin",
        "developer",
        "user",
        "guest",
        "test",
        "root",
        "team",
        "help",
        "info",
        "support",
        "community",
        "official",
        "contact",
        "media",
        "press",
        "home",
        "explore",
        "trending",
        "topics",
    }
    for h in handles:
        clean = h.strip().lstrip("@")
        if clean and len(clean) >= 3 and clean.lower() not in generic_noise:
            expanded_handles.add(clean)

    return expanded_handles, names


def extract_name_tokens(text: str) -> set[str]:
    """Extract individual lower-cased name tokens, filtering platform noise words."""
    noise = {
        "github",
        "linkedin",
        "x",
        "twitter",
        "devfolio",
        "huggingface",
        "profile",
        "posts",
        "photos",
        "videos",
        "activity",
        "overview",
        "machine",
        "learning",
        "artificial",
        "intelligence",
        "developer",
        "engineer",
        "software",
        "student",
        "at",
        "in",
        "and",
        "the",
        "for",
        "with",
        "dr",
        "mr",
        "ms",
        "mrs",
        "prof",
        "com",
        "https",
        "http",
        "www",
    }
    words = re.findall(r"[a-zA-Z]{2,}", text.lower())
    return {w for w in words if w not in noise}


def is_profile_consistent_with_subject(
    url: str,
    title: str | None,
    subject_tokens: set[str],
) -> bool:
    """Verify that a candidate personal profile URL belongs to the target subject.

    Rejects third-party profile pages (e.g. colleagues, sidebar connections, commenters)
    where the target's photo appeared from being falsely presented as the target's identity.
    """
    if not subject_tokens:
        return True
    if not is_social_profile_url(url):
        return True

    title_text = title or ""
    title_tokens = extract_name_tokens(title_text)
    parts = urlsplit(url)
    handle_tokens = extract_name_tokens(parts.path.replace("-", " ").replace("_", " "))
    cand_tokens = title_tokens | handle_tokens

    overlap = cand_tokens & subject_tokens
    if not overlap:
        # Check substring match for distinctive tokens
        full_str = f"{title_text.lower()} {parts.path.lower()}"
        return any(len(st) >= 5 and st in full_str for st in subject_tokens)

    # Distinctive token match (length >= 5, e.g. "bhattacharyya", "rakhaiya")
    if any(len(t) >= 5 for t in overlap):
        return True

    # Multi-token match (at least 2 overlapping tokens)
    if len(overlap) >= 2:
        return True

    # Single short token: only valid if subject has no distinctive multi-character tokens
    distinctive_subject = {t for t in subject_tokens if len(t) >= 5}
    return not (distinctive_subject and not (cand_tokens & distinctive_subject))


def discover_tech_profiles(
    seed_handles: set[str] | list[str],
    seed_names: set[str] | list[str] = (),
    *,
    client: httpx.Client | None = None,
    timeout_seconds: float = 8.0,
) -> list[TechProfile]:
    """Search and discover verified developer presences across tech platforms."""
    owns_client = client is None
    http = client or httpx.Client(
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
        follow_redirects=True,
        timeout=timeout_seconds,
    )
    discovered: list[TechProfile] = []
    seen_urls: set[str] = set()

    candidate_handles = set(seed_handles)
    candidate_names = set(seed_names)

    def _matches_seed_identity(cand_name: str | None) -> bool:
        if not cand_name or not seed_names:
            return True
        c_tokens = extract_name_tokens(cand_name)
        for sn in seed_names:
            s_tokens = extract_name_tokens(sn)
            distinctive = {t for t in s_tokens if len(t) >= 5}
            if distinctive:
                if c_tokens & distinctive:
                    return True
            elif len(c_tokens & s_tokens) >= min(2, len(s_tokens)):
                return True
        return False

    def add(
        platform: str,
        url: str,
        title: str,
        avatar_url: str | None = None,
        bio: str | None = None,
        handle: str | None = None,
        name: str | None = None,
    ) -> None:
        try:
            norm = normalize_page_url(url)
        except Exception:
            norm = url.rstrip("/")
        parts = urlsplit(norm)
        host = (parts.hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        if host in {"twitter.com", "x.com"}:
            host = "x.com"
            platform = "x"
        elif "linkedin.com" in host:
            platform = "linkedin"
        elif "devfolio.co" in host:
            platform = "devfolio"
        elif "huggingface.co" in host:
            platform = "huggingface"
        elif "github.com" in host:
            platform = "github"
        elif "kaggle.com" in host:
            platform = "kaggle"
        elif "devpost.com" in host:
            platform = "devpost"
        elif "leetcode.com" in host:
            platform = "leetcode"
        elif "medium.com" in host:
            platform = "medium"
        elif "instagram.com" in host:
            platform = "instagram"
        norm = urlunsplit((parts.scheme, host, parts.path.rstrip("/"), parts.query, ""))
        if norm in seen_urls:
            return
        seen_urls.add(norm)
        discovered.append(
            TechProfile(
                platform=platform,
                url=norm,
                title=title,
                avatar_url=avatar_url,
                bio=bio,
                handle=handle,
                name=name,
                verified=True,
            )
        )

    try:
        # 1. GitHub user probe
        for handle in list(candidate_handles):
            try:
                resp = http.get(f"https://api.github.com/users/{handle}")
                if resp.status_code == 200:
                    gh = resp.json()
                    name = gh.get("name") or handle
                    if seed_names and not _matches_seed_identity(gh.get("name")):
                        continue
                    candidate_names.add(name)
                    avatar = gh.get("avatar_url")
                    bio = gh.get("bio")
                    profile_url = gh.get("html_url", f"https://github.com/{handle}")
                    add(
                        "github",
                        profile_url,
                        f"{name} (@{handle}) · GitHub",
                        avatar,
                        bio,
                        handle,
                        name,
                    )

                    # Twitter handle from GitHub API
                    tw = gh.get("twitter_username")
                    if tw and str(tw).strip():
                        tw_clean = str(tw).strip().lstrip("@")
                        add(
                            "x",
                            f"https://x.com/{tw_clean}",
                            f"{name} (@{tw_clean}) · X",
                            handle=tw_clean,
                            name=name,
                        )
                        candidate_handles.add(tw_clean)

                    # Blog / Portfolio Website Deep Crawl
                    blog = gh.get("blog")
                    if blog and isinstance(blog, str) and blog.strip():
                        blog_url = blog.strip()
                        if not blog_url.startswith("http"):
                            blog_url = f"https://{blog_url}"
                        try:
                            blog_resp = http.get(blog_url)
                            if blog_resp.status_code == 200:
                                b_text = blog_resp.text
                                og_m = re.search(
                                    r'<meta\s+(?:property|name)=["\']og:image["\']\s+content=["\']([^"\']+)["\']',
                                    b_text,
                                    re.I,
                                )
                                og_avatar = og_m.group(1) if og_m else None
                                if og_avatar and not og_avatar.startswith("http"):
                                    og_avatar = urljoin(blog_url, og_avatar)
                                add(
                                    "web",
                                    blog_url,
                                    f"{name} · Portfolio & Blog",
                                    avatar_url=og_avatar,
                                    name=name,
                                )
                                for sm in re.findall(
                                    r"https?://(?:www\.)?(?:linkedin\.com/in/[a-zA-Z0-9_-]+|"
                                    r"devfolio\.co/@[a-zA-Z0-9_-]+|"
                                    r"huggingface\.co/[a-zA-Z0-9_-]+|"
                                    r"kaggle\.com/[a-zA-Z0-9_-]+|"
                                    r"devpost\.com/[a-zA-Z0-9_-]+|"
                                    r"leetcode\.com/(?:u/)?[a-zA-Z0-9_-]+|"
                                    r"(?:x\.com|twitter\.com)/[a-zA-Z0-9_-]+)",
                                    b_text,
                                    re.I,
                                ):
                                    add("web", sm, f"{name} · Linked Profile", name=name)
                        except Exception:
                            pass

                    # README link parsing for connected social badges
                    for branch in ("main", "master"):
                        r_url = f"https://raw.githubusercontent.com/{handle}/{handle}/{branch}/README.md"
                        readme_resp = http.get(r_url)
                        if readme_resp.status_code == 200:
                            text = readme_resp.text
                            # LinkedIn
                            for lm in re.findall(
                                r"https?://(?:www\.)?linkedin\.com/in/([a-zA-Z0-9_-]+)",
                                text,
                                re.I,
                            ):
                                add(
                                    "linkedin",
                                    f"https://www.linkedin.com/in/{lm}",
                                    f"{name} · LinkedIn",
                                    None,
                                    None,
                                    lm,
                                    name,
                                )
                                candidate_handles.add(lm)
                            # Devfolio
                            for dm in re.findall(r"https?://devfolio\.co/@([a-zA-Z0-9_-]+)", text):
                                candidate_handles.add(dm)
                            # Hugging Face
                            for hm in re.findall(
                                r"https?://huggingface\.co/([a-zA-Z0-9_-]+)", text
                            ):
                                candidate_handles.add(hm)
                            # Kaggle
                            for km in re.findall(
                                r"https?://(?:www\.)?kaggle\.com/([a-zA-Z0-9_-]+)", text, re.I
                            ):
                                if km.lower() not in {"code", "datasets", "learn", "competitions"}:
                                    add(
                                        "kaggle",
                                        f"https://www.kaggle.com/{km}",
                                        f"{name} (@{km}) · Kaggle",
                                        handle=km,
                                        name=name,
                                    )
                                    candidate_handles.add(km)
                            # Devpost
                            for dpm in re.findall(
                                r"https?://(?:www\.)?devpost\.com/([a-zA-Z0-9_-]+)", text, re.I
                            ):
                                if dpm.lower() not in {"software", "hackathons"}:
                                    add(
                                        "devpost",
                                        f"https://devpost.com/{dpm}",
                                        f"{name} (@{dpm}) · Devpost",
                                        handle=dpm,
                                        name=name,
                                    )
                                    candidate_handles.add(dpm)
                            # LeetCode
                            for lcm in re.findall(
                                r"https?://(?:www\.)?leetcode\.com/(?:u/)?([a-zA-Z0-9_-]+)",
                                text,
                                re.I,
                            ):
                                if lcm.lower() not in {"problems", "contest", "discuss", "explore"}:
                                    add(
                                        "leetcode",
                                        f"https://leetcode.com/u/{lcm}",
                                        f"{name} (@{lcm}) · LeetCode",
                                        handle=lcm,
                                        name=name,
                                    )
                                    candidate_handles.add(lcm)
                            # X / Twitter
                            for xm in re.findall(
                                r"https?://(?:x\.com|twitter\.com)/([a-zA-Z0-9_-]+)",
                                text,
                                re.I,
                            ):
                                if xm.lower() not in {"intent", "share", "home"}:
                                    add(
                                        "x",
                                        f"https://x.com/{xm}",
                                        f"{name} (@{xm}) · X",
                                        None,
                                        None,
                                        xm,
                                        name,
                                    )
                            break
            except Exception:
                pass

        # 2. Devfolio probe
        for handle in list(candidate_handles):
            try:
                resp = http.get(f"https://devfolio.co/@{handle}")
                if resp.status_code == 200:
                    match = re.search(
                        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
                        resp.text,
                    )
                    if match:
                        data = json.loads(match.group(1))
                        queries = (
                            data.get("props", {})
                            .get("pageProps", {})
                            .get("dehydratedState", {})
                            .get("queries", [])
                        )
                        for q in queries:
                            users = q.get("state", {}).get("data", {}).get("users", [])
                            if isinstance(users, list) and users:
                                u = users[0]
                                first = u.get("first_name", "")
                                last = u.get("last_name", "")
                                u_name = f"{first} {last}".strip() or handle
                                if seed_names and not _matches_seed_identity(u_name):
                                    continue
                                u_bio = u.get("short_bio") or u.get("bio")
                                u_avatar = u.get("profile_picture") or u.get("avatar_url")
                                candidate_names.add(u_name)
                                add(
                                    "devfolio",
                                    f"https://devfolio.co/@{handle}",
                                    f"{u_name} (@{handle}) · Devfolio",
                                    u_avatar,
                                    u_bio,
                                    handle,
                                    u_name,
                                )
                                for p in u.get("profiles", []):
                                    val = p.get("value")
                                    p_name = p.get("profile", {}).get("name", "")
                                    if val and str(val).startswith("http"):
                                        plat = p_name.lower() if p_name else "web"
                                        add(plat, val, f"{u_name} · {p_name}", name=u_name)
            except Exception:
                pass

        # 3. Hugging Face probe
        for handle in list(candidate_handles):
            try:
                resp = http.get(f"https://huggingface.co/api/users/{handle}/overview")
                if resp.status_code == 200:
                    hf = resp.json()
                    name = hf.get("fullname") or handle
                    if seed_names and not _matches_seed_identity(hf.get("fullname")):
                        continue
                    candidate_names.add(name)
                    avatar = hf.get("avatarUrl")
                    if avatar and avatar.startswith("/"):
                        avatar = f"https://huggingface.co{avatar}"
                    add(
                        "huggingface",
                        f"https://huggingface.co/{handle}",
                        f"{name} (@{handle}) · Hugging Face",
                        avatar,
                        None,
                        handle,
                        name,
                    )
            except Exception:
                pass

        # 4. Kaggle probe
        for handle in list(candidate_handles):
            try:
                resp = http.get(f"https://www.kaggle.com/{handle}")
                if resp.status_code == 200:
                    add(
                        "kaggle",
                        f"https://www.kaggle.com/{handle}",
                        f"{handle} · Kaggle",
                        handle=handle,
                    )
            except Exception:
                pass

        # 5. Devpost probe
        for handle in list(candidate_handles):
            try:
                resp = http.get(f"https://devpost.com/{handle}")
                if resp.status_code == 200:
                    add(
                        "devpost",
                        f"https://devpost.com/{handle}",
                        f"{handle} · Devpost",
                        handle=handle,
                    )
            except Exception:
                pass

        # 6. Search-based discovery for known candidate names
        for name in list(candidate_names):
            if len(name.split()) >= 2:
                try:
                    q = f'site:huggingface.co "{name}"'
                    resp = http.get(
                        f"https://html.duckduckgo.com/html/?q={q}",
                        headers={"User-Agent": "Mozilla/5.0"},
                    )
                    if resp.status_code == 200:
                        hf_handles = re.findall(
                            r"huggingface\.co/([a-zA-Z0-9_-]+)",
                            resp.text,
                        )
                        for h in hf_handles:
                            if h.lower() in {
                                "models",
                                "datasets",
                                "spaces",
                                "docs",
                                "blog",
                                "pricing",
                            }:
                                continue
                            ov_resp = http.get(f"https://huggingface.co/api/users/{h}/overview")
                            if ov_resp.status_code == 200:
                                ov = ov_resp.json()
                                ov_name = ov.get("fullname", "")
                                if (
                                    name.lower() in ov_name.lower()
                                    or ov_name.lower() in name.lower()
                                ):
                                    avatar = ov.get("avatarUrl")
                                    if avatar and avatar.startswith("/"):
                                        avatar = f"https://huggingface.co{avatar}"
                                    add(
                                        "huggingface",
                                        f"https://huggingface.co/{h}",
                                        f"{ov_name} (@{h}) · Hugging Face",
                                        avatar,
                                        None,
                                        h,
                                        ov_name,
                                    )
                                    break
                except Exception:
                    pass

    finally:
        if owns_client:
            http.close()

    return discovered


def _resolve_candidate_avatar(url: str, *, http: httpx.Client | None = None) -> str | None:
    """Extract authentic profile avatar URL for developer and knowledge platforms."""
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]

        # 1. GitHub avatar
        if "github.com" in host:
            gh_match = re.search(r"github\.com/([a-zA-Z0-9_-]+)", url, re.I)
            if gh_match:
                handle = gh_match.group(1)
                if handle.lower() not in {
                    "topics",
                    "trending",
                    "explore",
                    "settings",
                    "about",
                    "orgs",
                    "features",
                    "pricing",
                    "security",
                }:
                    return f"https://github.com/{handle}.png"

        # 2. Wikipedia lead portrait
        if "wikipedia.org" in host and http:
            wiki_m = re.search(r"([a-z]{2,3})\.wikipedia\.org/wiki/([^/?#]+)", url, re.I)
            if wiki_m:
                lang, article = wiki_m.group(1), wiki_m.group(2)
                try:
                    resp = http.get(
                        f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/{article}",
                        timeout=4.0,
                    )
                    if resp.status_code == 200:
                        wdata = resp.json()
                        orig = wdata.get("originalimage", {}).get("source")
                        thumb = wdata.get("thumbnail", {}).get("source")
                        return orig or thumb
                except Exception:
                    pass

        # 3. Hugging Face avatar
        if "huggingface.co" in host and http:
            hf_m = re.search(r"huggingface\.co/([a-zA-Z0-9_-]+)", url, re.I)
            if hf_m:
                handle = hf_m.group(1)
                if handle.lower() not in {
                    "models",
                    "datasets",
                    "spaces",
                    "docs",
                    "blog",
                    "pricing",
                }:
                    try:
                        resp = http.get(
                            f"https://huggingface.co/api/users/{handle}/overview",
                            timeout=4.0,
                        )
                        if resp.status_code == 200:
                            av = resp.json().get("avatarUrl")
                            if av:
                                return f"https://huggingface.co{av}" if av.startswith("/") else av
                    except Exception:
                        pass

        # 4. OpenGraph image for public web pages (excluding login-walled social networks)
        if http and not any(k in host for k in ("linkedin.com", "facebook.com", "instagram.com")):
            try:
                resp = http.get(url, timeout=3.5)
                if resp.status_code == 200:
                    m = re.search(
                        r'<meta\s+(?:property|name)=["\'](?:og:image|twitter:image)["\']\s+content=["\']([^"\']+)["\']',
                        resp.text,
                        re.I,
                    )
                    if m and m.group(1).startswith("http"):
                        return m.group(1)
            except Exception:
                pass
    except Exception:
        pass

    return None


def search_wikipedia_profile(
    name: str,
    *,
    client: httpx.Client | None = None,
    timeout_seconds: float = 6.0,
) -> SearchCandidate | None:
    """Search Wikipedia for subject identity and extract article and lead portrait."""
    if not name or len(name.strip().split()) < 2:
        return None
    clean = name.strip()
    owns_client = client is None
    http = client or httpx.Client(
        headers={"User-Agent": "FaceProof/1.0 (biometric verification research)"},
        follow_redirects=True,
        timeout=timeout_seconds,
    )
    try:
        resp = http.get(
            "https://en.wikipedia.org/w/api.php",
            params={
                "action": "opensearch",
                "search": clean,
                "limit": 2,
                "namespace": 0,
                "format": "json",
            },
        )
        if resp.status_code == 200:
            data = resp.json()
            if len(data) >= 4 and data[1] and data[3]:
                title = data[1][0]
                wiki_url = data[3][0]
                target_tokens = extract_name_tokens(clean)
                title_tokens = extract_name_tokens(title)
                distinctive = {t for t in target_tokens if len(t) >= 5}
                is_match = (
                    bool(distinctive and (title_tokens & distinctive))
                    or len(title_tokens & target_tokens) >= 2
                )
                if is_match:
                    sum_resp = http.get(
                        f"https://en.wikipedia.org/api/rest_v1/page/summary/{title}"
                    )
                    if sum_resp.status_code == 200:
                        sdata = sum_resp.json()
                        img_url = sdata.get("originalimage", {}).get("source") or sdata.get(
                            "thumbnail", {}
                        ).get("source")
                        return SearchCandidate(
                            provider="wikipedia",
                            rank=0,
                            page_url=wiki_url,
                            normalized_url=wiki_url,
                            title=f"{sdata.get('title', title)} · Wikipedia",
                            source="wikipedia",
                            image_url=img_url,
                            thumbnail_url=img_url,
                            provider_score=1.0,
                            exact_match=False,
                            result_type="wikipedia_profile",
                            post_id=None,
                        )
    except Exception:
        pass
    finally:
        if owns_client:
            http.close()
    return None


def search_profiles_by_name(
    name: str,
    *,
    api_key: str | None = None,
    client: httpx.Client | None = None,
    timeout_seconds: float = 12.0,
    primary_avatar: str | None = None,
) -> list[SearchCandidate]:
    """Perform targeted secondary name search across professional & tech networks."""
    if not name or len(name.strip().split()) < 2:
        return []
    if not api_key:
        return []

    owns_client = client is None
    http = client or httpx.Client(
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
        follow_redirects=True,
        timeout=timeout_seconds,
    )
    candidates: list[SearchCandidate] = []
    clean_name = name.strip()
    target_tokens = extract_name_tokens(clean_name)
    distinctive_tokens = {t for t in target_tokens if len(t) >= 5}
    query = (
        f'"{clean_name}" '
        "(site:github.com OR site:devfolio.co OR "
        "site:huggingface.co OR site:kaggle.com OR site:devpost.com OR site:leetcode.com "
        "OR site:wikipedia.org)"
    )
    try:
        resp = http.get(
            "https://serpapi.com/search.json",
            params={
                "engine": "google",
                "q": query,
                "api_key": api_key,
                "num": 10,
            },
        )
        if resp.status_code == 200:
            data = resp.json()
            for idx, item in enumerate(data.get("organic_results", []), start=1):
                link = item.get("link")
                if not link or not link.startswith("https://"):
                    continue
                title = item.get("title") or ""
                # Strict name filtering: reject strangers whose title does not match subject
                title_tokens = extract_name_tokens(title)
                overlap = title_tokens & target_tokens
                if distinctive_tokens:
                    if not (overlap & distinctive_tokens):
                        continue
                elif len(overlap) < min(2, len(target_tokens)):
                    continue

                try:
                    norm = normalize_page_url(link)
                except ValueError:
                    norm = link
                avatar = _resolve_candidate_avatar(norm, http=http)
                cand = SearchCandidate(
                    provider="name-search",
                    rank=idx,
                    page_url=link,
                    normalized_url=norm,
                    title=title or norm,
                    source=platform_name(norm) or "web",
                    image_url=avatar,
                    thumbnail_url=avatar,
                    provider_score=0.9,
                    exact_match=False,
                    result_type="name_search_profile",
                    post_id=None,
                )
                candidates.append(cand)
    except Exception:
        pass
    finally:
        if owns_client:
            http.close()

    return candidates
