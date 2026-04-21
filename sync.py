from __future__ import annotations

import logging
import os

import yaml
from github import Auth, Github, GithubException, RateLimitExceededException
from github.Organization import Organization
from github.Repository import Repository
from github.Team import Team

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CONFIG_FILE = os.getenv("GORG_CONFIG_FILE", "gorg.yaml")
REPORT_FILE = os.getenv("GORG_REPORT_FILE", "gorg.md")

VALID_PERMISSIONS = frozenset({"pull", "triage", "write", "maintain", "admin"})

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _coerce(value: dict | list | None, kind: type) -> dict | list:
    """Return value if it matches kind, otherwise return an empty instance."""
    return value if isinstance(value, kind) else kind()


def _gh_error(exc: GithubException) -> str:
    """Extract a readable message from a GithubException."""
    return exc.data.get("message", str(exc)) if isinstance(exc.data, dict) else str(exc)


# ---------------------------------------------------------------------------
# GitHub App authentication
# ---------------------------------------------------------------------------


def _require_env(name: str) -> str:
    """Return the value of an env var, raising EnvironmentError if missing or empty."""
    value = os.getenv(name, "").strip()
    if not value:
        raise EnvironmentError(
            f"Required environment variable '{name}' is missing or empty."
        )
    return value


def _load_private_key() -> str:
    """Load the private key from a file path or inline env var value."""
    raw = _require_env("GORG_APP_PRIVATE_KEY")
    if os.path.isfile(raw):
        with open(raw) as f:
            return f.read()
    return raw.replace("\\n", "\n")


def get_github_client() -> Github:
    """Authenticate as a GitHub App installation and return a Github client."""
    app_id = int(_require_env("GORG_APP_ID"))
    installation_id = int(_require_env("GORG_INSTALLATION_ID"))
    private_key = _load_private_key()

    auth = Auth.AppInstallationAuth(
        Auth.AppAuth(app_id, private_key),
        installation_id,
    )
    return Github(auth=auth)


def validate_org_access(gt: Github, org_name: str) -> Organization:
    """Return the Organization object or exit early with a clear message."""
    try:
        return gt.get_organization(org_name)
    except GithubException as exc:
        raise SystemExit(
            f"Cannot access org '{org_name}'. "
            "Check the organization name and that the app is installed on it. "
            f"Details: {_gh_error(exc)}"
        ) from exc


# ---------------------------------------------------------------------------
# Config loading & validation
# ---------------------------------------------------------------------------


def _validate_repo_permissions(repo_name: str, repo_config: dict, org_name: str) -> None:
    """Raise ValueError if any permission value in a repo config is invalid."""
    for kind in ("teams", "users"):
        for name, permission in _coerce(repo_config.get(kind), dict).items():
            if permission not in VALID_PERMISSIONS:
                raise ValueError(
                    f"Invalid permission '{permission}' for {kind[:-1]} '{name}' "
                    f"in repo '{repo_name}' (org: '{org_name}'). "
                    f"Valid values: {', '.join(sorted(VALID_PERMISSIONS))}"
                )


def load_config(path: str) -> dict:
    """Load and validate the gorg.yaml config file."""
    with open(path) as f:
        config = yaml.safe_load(f)

    if not isinstance(config, dict):
        raise ValueError(f"Config file '{path}' is empty or not valid YAML.")

    if not config.get("organization"):
        raise ValueError("Config must define an 'organization' field.")

    # Prevent the action repo itself from being managed via gorg.yaml.
    # GITHUB_REPOSITORY is set automatically by GitHub Actions (e.g. "your-org/gorg-as-code").
    current_repo = os.getenv("GITHUB_REPOSITORY", "")
    if current_repo:
        current_repo_name = current_repo.split("/")[-1]
        if current_repo_name in _coerce(config.get("repos"), dict):
            raise ValueError(
                f"Repo '{current_repo_name}' cannot manage itself. "
                "Remove it from the repos section in gorg.yaml."
            )

    for repo_name, repo_config in _coerce(config.get("repos"), dict).items():
        _validate_repo_permissions(
            repo_name,
            _coerce(repo_config, dict),
            config["organization"],
        )

    return config


# ---------------------------------------------------------------------------
# Sync — teams
# ---------------------------------------------------------------------------


def _sync_team_members(gt: Github, team: Team, team_name: str, desired: dict) -> None:
    """Add, update, and remove team members to match the desired state."""
    maintainers = _coerce(desired.get("maintainers"), list)
    members = _coerce(desired.get("members"), list)
    desired_logins = set(maintainers + members)

    for current in team.get_members():
        if current.login not in desired_logins:
            logger.info("  Removing '%s' from team '%s'", current.login, team_name)
            team.remove_membership(current)

    for role, login in [*[("maintainer", u) for u in maintainers], *[("member", u) for u in members]]:
        try:
            team.add_membership(gt.get_user(login), role=role)
            logger.info("  Set '%s' as %s in '%s'", login, role, team_name)
        except GithubException as exc:
            logger.error(
                "  Failed to set '%s' as %s in '%s': %s",
                login, role, team_name, _gh_error(exc),
            )


def sync_team(gt: Github, org: Organization, team_name: str, desired: dict) -> None:
    """Sync a single team's membership to match the desired state."""
    try:
        team = org.get_team_by_slug(team_name)
    except GithubException as exc:
        level = logger.warning if exc.status == 404 else logger.error
        level(
            "  Team '%s' not found in org '%s' — skipping. "
            "Ensure the name matches the GitHub slug: %s",
            team_name, org.login, _gh_error(exc),
        )
        return

    _sync_team_members(gt, team, team_name, desired)


# ---------------------------------------------------------------------------
# Sync — repos
# ---------------------------------------------------------------------------


def _sync_repo_teams(
    org: Organization,
    repo: Repository,
    repo_name: str,
    desired_teams: dict,
    managed_teams: frozenset[str],
) -> None:
    """Remove stale team access and apply desired team permissions on a repo."""
    for team_name in managed_teams - desired_teams.keys():
        try:
            team = org.get_team_by_slug(team_name)
            if team.has_in_repos(repo):
                logger.info("  Removing team '%s' from repo '%s'", team_name, repo_name)
                team.remove_from_repos(repo)
        except GithubException as exc:
            logger.error(
                "  Failed to remove team '%s' from '%s': %s",
                team_name, repo_name, _gh_error(exc),
            )

    for team_name, permission in desired_teams.items():
        try:
            team = org.get_team_by_slug(team_name)
            logger.info("  Setting team '%s' to '%s' on '%s'", team_name, permission, repo_name)
            team.update_team_repository(repo, permission)
        except GithubException as exc:
            logger.error(
                "  Failed to update team '%s' on '%s': %s",
                team_name, repo_name, _gh_error(exc),
            )


def _sync_repo_users(repo: Repository, repo_name: str, desired_users: dict) -> None:
    """Remove stale direct collaborators and apply desired user permissions on a repo.

    Uses affiliation='direct' to avoid touching members added via teams.
    """
    for collaborator in repo.get_collaborators(affiliation="direct"):
        if collaborator.login not in desired_users:
            try:
                logger.info("  Removing user '%s' from repo '%s'", collaborator.login, repo_name)
                repo.remove_from_collaborators(collaborator.login)
            except GithubException as exc:
                logger.error(
                    "  Failed to remove user '%s' from '%s': %s",
                    collaborator.login, repo_name, _gh_error(exc),
                )

    for login, permission in desired_users.items():
        try:
            logger.info("  Setting user '%s' to '%s' on '%s'", login, permission, repo_name)
            repo.add_to_collaborators(login, permission=permission)
        except GithubException as exc:
            logger.error(
                "  Failed to add user '%s' to '%s': %s",
                login, repo_name, _gh_error(exc),
            )


def sync_repo(
    org: Organization,
    repo_name: str,
    desired: dict,
    managed_teams: frozenset[str],
) -> None:
    """Sync a single repo's team and user permissions to match the desired state."""
    try:
        repo = org.get_repo(repo_name)
    except GithubException as exc:
        level = logger.warning if exc.status == 404 else logger.error
        level(
            "  Repo '%s' not found in org '%s' — skipping: %s",
            repo_name, org.login, _gh_error(exc),
        )
        return

    _sync_repo_teams(org, repo, repo_name, _coerce(desired.get("teams"), dict), managed_teams)
    _sync_repo_users(repo, repo_name, _coerce(desired.get("users"), dict))


# ---------------------------------------------------------------------------
# Sync — org
# ---------------------------------------------------------------------------


def sync_org(gt: Github, org: Organization, config: dict) -> None:
    """Sync the organization's teams and repos to match the desired state."""
    logger.info("Syncing organization '%s'..", org.login)

    desired_teams = _coerce(config.get("teams"), dict)
    desired_repos = _coerce(config.get("repos"), dict)
    managed_teams = frozenset(desired_teams)

    for team_name, team_config in desired_teams.items():
        logger.info("Syncing team '%s'..", team_name)
        sync_team(gt, org, team_name, _coerce(team_config, dict))

    for repo_name, repo_config in desired_repos.items():
        logger.info("Syncing repo '%s'..", repo_name)
        sync_repo(org, repo_name, _coerce(repo_config, dict), managed_teams)


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------


def _teams_table(teams: dict) -> list[str]:
    lines = [
        "## Teams",
        "| Team | Maintainers | Members |",
        "|------|-------------|---------|",
    ]
    for team_name, team_config in teams.items():
        cfg = _coerce(team_config, dict)
        maintainers = ", ".join(_coerce(cfg.get("maintainers"), list)) or "-"
        members = ", ".join(_coerce(cfg.get("members"), list)) or "-"
        lines.append(f"| {team_name} | {maintainers} | {members} |")
    return lines


def _permissions_table(repos: dict) -> list[str]:
    lines = [
        "\n## Repository Permissions",
        "| Repository | Type | Name | Permission |",
        "|------------|------|------|------------|",
    ]
    for repo_name, repo_config in repos.items():
        cfg = _coerce(repo_config, dict)
        teams = _coerce(cfg.get("teams"), dict)
        users = _coerce(cfg.get("users"), dict)

        if not teams and not users:
            lines.append(f"| {repo_name} | - | - | - |")
            continue

        for name, permission in teams.items():
            lines.append(f"| {repo_name} | team | {name} | {permission} |")
        for login, permission in users.items():
            lines.append(f"| {repo_name} | user | {login} | {permission} |")

    return lines


def generate_report(config: dict, output_file: str = REPORT_FILE) -> None:
    """Write a markdown report of the current desired state to output_file."""
    lines = [
        "> Auto-generated by gorg-as-code. Do not edit manually.\n",
        f"# Organization: {config['organization']}\n",
        *_teams_table(_coerce(config.get("teams"), dict)),
        *_permissions_table(_coerce(config.get("repos"), dict)),
    ]

    with open(output_file, "w") as f:
        f.write("\n".join(lines) + "\n")

    logger.info("Report written to '%s'", output_file)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="[%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler()],
    )

    config = load_config(CONFIG_FILE)

    try:
        gt = get_github_client()
    except EnvironmentError as exc:
        logger.error("Authentication failed: %s", exc)
        raise SystemExit(1)

    org = validate_org_access(gt, config["organization"])

    rate_limit_hit = False
    try:
        sync_org(gt, org, config)
    except RateLimitExceededException:
        logger.error(
            "GitHub API rate limit exceeded. "
            "Re-run the workflow when the limit resets."
        )
        rate_limit_hit = True

    generate_report(config)

    if rate_limit_hit:
        raise SystemExit(1)


if __name__ == "__main__":
    main()