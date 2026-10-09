"""Stable, tested conversion boundary for aiograpi profile objects."""
def parse_profile(raw):
    def count(*names):
        for name in names:
            value = getattr(raw, name, None)
            if value is not None:
                return max(0, int(value))
        return None

    result = {
        "instagram_pk": str(getattr(raw, "pk", "") or ""),
        "username": str(getattr(raw, "username", "") or "").strip().lstrip("@").lower(),
        "full_name": str(getattr(raw, "full_name", "") or "")[:250],
        "is_private": bool(getattr(raw, "is_private", False)),
        "is_verified": bool(getattr(raw, "is_verified", False)),
        "followers_count": count("follower_count", "followers_count"),
        "following_count": count("following_count", "followings_count"),
        "posts_count": count("media_count", "posts_count", "post_count"),
        "reels_count": count("clips_count", "reels_count", "reel_count"),
    }
    result["media_count"] = result["posts_count"]
    return result


def completeness_score(profile):
    """Data quality, not a claim of exhaustive Instagram coverage."""
    return sum(profile.get(field) is not None for field in
               ("instagram_pk", "username", "full_name", "followers_count", "following_count",
                "posts_count", "reels_count")) / 7
