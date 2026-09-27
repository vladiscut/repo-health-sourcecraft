from health.models import Profile, Repository, UserRepositoryAccess


def make_repo(**kwargs) -> Repository:
    number = Repository.objects.count() + 1
    org = kwargs.pop("org_slug", "acme")
    slug = kwargs.pop("repo_slug", f"repo-{number}")
    defaults = {
        "org_slug": org,
        "repo_slug": slug,
        "description": "",
        "language": "Python",
        "rating_value": 0,
        "url": f"https://sourcecraft.dev/{org}/{slug}",
        "sourcecraft_id": kwargs.pop("sourcecraft_id", f"sc-{number}"),
        "visibility": Repository.VisibilityType.PUBLIC,
    }
    defaults.update(kwargs)
    return Repository.objects.create(**defaults)


def make_profile(user, **kwargs) -> Profile:
    defaults = {
        "user": user,
        "ya_id": kwargs.pop("ya_id", f"ya-{user.pk}"),
        "sourcecraft_username": user.username,
    }
    defaults.update(kwargs)
    return Profile.objects.create(**defaults)


def grant_access(user, repo) -> UserRepositoryAccess:
    return UserRepositoryAccess.objects.create(user=user, repository=repo)
