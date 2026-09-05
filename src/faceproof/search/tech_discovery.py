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

    # Normalized handle variants
    expanded_handles: set[str] = set()
    for h in handles:
        clean = h.strip().lstrip("@")
        if clean:
            expanded_handles.add(clean)
            stripped = re.sub(r"\d+$", "", clean)
            if len(stripped) >= 3:
                expanded_handles.add(stripped)

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

    # Direct token overlap
    if cand_tokens & subject_tokens:
        return True

    # Substring in title or handle path
    full_str = f"{title_text.lower()} {parts.path.lower()}"
    for st in subject_tokens:
        if len(st) >= 3 and st in full_str:
            return True

    # If the candidate profile has identified tokens belonging to someone else, reject
    return not cand_tokens


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
                                u_bio = u.get("short_bio") or u.get("bio")
                                candidate_names.add(u_name)
                                add(
                                    "devfolio",
                                    f"https://devfolio.co/@{handle}",
                                    f"{u_name} (@{handle}) · Devfolio",
                                    None,
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

        # Propagate verified photo avatar if available across identity cluster
        primary_avatar = next(
            (
                p.avatar_url
                for p in discovered
                if p.avatar_url and not p.avatar_url.endswith(".svg")
            ),
            next((p.avatar_url for p in discovered if p.avatar_url), None),
        )
        if primary_avatar:
            discovered = [
                TechProfile(
                    platform=p.platform,
                    url=p.url,
                    title=p.title,
                    avatar_url=p.avatar_url or primary_avatar,
                    bio=p.bio,
                    handle=p.handle,
                    name=p.name,
                    verified=p.verified,
                )
                for p in discovered
            ]
    finally:
        if owns_client:
            http.close()

    return discovered


def _resolve_profile_avatar(url: str, *, fallback_avatar: str | None = None) -> str | None:
    """Resolve direct avatar URL for common developer/social platforms."""
    gh_match = re.search(r"github\.com/([a-zA-Z0-9_-]+)", url, re.I)
    if gh_match:
        gh_handle = gh_match.group(1)
        if gh_handle.lower() not in {
            "topics",
            "trending",
            "explore",
            "settings",
            "about",
            "orgs",
            "features",
        }:
            return f"https://github.com/{gh_handle}.png"

    return fallback_avatar


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
    query = (
        f'"{clean_name}" '
        "(site:github.com OR site:linkedin.com/in OR site:devfolio.co OR "
        "site:huggingface.co OR site:kaggle.com OR site:devpost.com OR site:leetcode.com)"
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
                try:
                    norm = normalize_page_url(link)
                except ValueError:
                    norm = link
                title = item.get("title") or norm
                avatar = _resolve_profile_avatar(norm, fallback_avatar=primary_avatar)
                cand = SearchCandidate(
                    provider="name-search",
                    rank=idx,
                    page_url=link,
                    normalized_url=norm,
                    title=title,
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
