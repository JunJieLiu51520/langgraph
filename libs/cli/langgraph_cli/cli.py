import json
import pathlib
import shutil
import sys
from typing import Callable, Optional, Sequence

import click
import click.exceptions
from click import secho

import langgraph_cli.config
import langgraph_cli.docker
from langgraph_cli.analytics import log_command
from langgraph_cli.config import Config
from langgraph_cli.constants import DEFAULT_CONFIG, DEFAULT_PORT
from langgraph_cli.docker import DockerCapabilities
from langgraph_cli.exec import Runner, subp_exec
from langgraph_cli.progress import Progress
from langgraph_cli.version import __version__

OPT_DOCKER_COMPOSE = click.option(
    "--docker-compose",
    "-d",
    help="Advanced: Path to docker-compose.yml file with additional services to launch.",
    type=click.Path(
        exists=True,
        file_okay=True,
        dir_okay=False,
        resolve_path=True,
        path_type=pathlib.Path,
    ),
)
OPT_CONFIG = click.option(
    "--config",
    "-c",
    help="""Path to configuration file declaring dependencies, graphs and environment variables.

    \b
    Config file must be a JSON file that has the following keys:
    - "dependencies": array of dependencies for langgraph API server. Dependencies can be one of the following:
      - ".", which would look for local python packages, as well as pyproject.toml, setup.py or requirements.txt in the app directory
      - "./local_package"
      - "<package_name>
    - "graphs": mapping from graph ID to path where the compiled graph is defined, i.e. ./your_package/your_file.py:variable, where
        "variable" is an instance of langgraph.graph.graph.CompiledGraph
    - "env": (optional) path to .env file or a mapping from environment variable to its value
    - "python_version": (optional) 3.11 or 3.12. Defaults to 3.11
    - "pip_config_file": (optional) path to pip config file
    - "dockerfile_lines": (optional) array of additional lines to add to Dockerfile following the import from parent image

    \b
    Example:
        langgraph up -c langgraph.json

    \b
    Example:
    {
        "dependencies": [
            "langchain_openai",
            "./your_package"
        ],
        "graphs": {
            "my_graph_id": "./your_package/your_file.py:variable"
        },
        "env": "./.env"
    }

    \b
    Example:
    {
        "python_version": "3.11",
        "dependencies": [
            "langchain_openai",
            "."
        ],
        "graphs": {
            "my_graph_id": "./your_package/your_file.py:variable"
        },
        "env": {
            "OPENAI_API_KEY": "secret-key"
        }
    }

    Defaults to looking for langgraph.json in the current directory.""",
    default=DEFAULT_CONFIG,
    type=click.Path(
        exists=True,
        file_okay=True,
        dir_okay=False,
        resolve_path=True,
        path_type=pathlib.Path,
    ),
)
OPT_PORT = click.option(
    "--port",
    "-p",
    type=int,
    default=DEFAULT_PORT,
    show_default=True,
    help="""
    Port to expose.

    \b
    Example:
        langgraph up --port 8000
    \b
    """,
)
OPT_RECREATE = click.option(
    "--recreate/--no-recreate",
    default=False,
    show_default=True,
    help="Recreate containers even if their configuration and image haven't changed",
)
OPT_PULL = click.option(
    "--pull/--no-pull",
    default=True,
    show_default=True,
    help="""
    Pull latest images. Use --no-pull for running the server with locally-built images.

    \b
    Example:
        langgraph up --no-pull
    \b
    """,
)
OPT_VERBOSE = click.option(
    "--verbose",
    is_flag=True,
    default=False,
    help="Show more output from the server logs",
)
OPT_WATCH = click.option("--watch", is_flag=True, help="Restart on file changes")
OPT_DEBUGGER_PORT = click.option(
    "--debugger-port",
    type=int,
    help="Pull the debugger image locally and serve the UI on specified port",
)
OPT_DEBUGGER_BASE_URL = click.option(
    "--debugger-base-url",
    type=str,
    help="URL used by the debugger to access LangGraph API. Defaults to http://127.0.0.1:[PORT]",
)

OPT_POSTGRES_URI = click.option(
    "--postgres-uri",
    help="Postgres URI to use for the database. Defaults to launching a local database",
)


@click.group()
@click.version_option(version=__version__, prog_name="LangGraph CLI")
def cli():
    pass


@OPT_RECREATE
@OPT_PULL
@OPT_PORT
@OPT_DOCKER_COMPOSE
@OPT_CONFIG
@OPT_VERBOSE
@OPT_DEBUGGER_PORT
@OPT_DEBUGGER_BASE_URL
@OPT_WATCH
@OPT_POSTGRES_URI
@click.option(
    "--wait",
    is_flag=True,
    help="Wait for services to start before returning. Implies --detach",
)
@cli.command(
    help=(
        "🚀 Start langgraph API server. For local testing, requires a LangSmith API "
        "key with access to LangGraph Cloud closed beta. Requires a license key for "
        "production use."
    )
)
@log_command
def up(
    config: pathlib.Path,
    docker_compose: Optional[pathlib.Path],
    port: int,
    recreate: bool,
    pull: bool,
    watch: bool,
    wait: bool,
    verbose: bool,
    debugger_port: Optional[int],
    debugger_base_url: Optional[str],
    postgres_uri: Optional[str],
):
    click.secho("Starting LangGraph API server...", fg="green")
    click.secho(
        """For local dev, requires env var LANGSMITH_API_KEY with access to LangGraph Cloud closed beta.
For production use, requires a license key in env var LANGGRAPH_CLOUD_LICENSE_KEY.""",
        fg="red",
    )
    with Runner() as runner, Progress(message="Pulling...") as set:
        capabilities = langgraph_cli.docker.check_capabilities(runner)
        args, stdin = prepare(
            runner,
            capabilities=capabilities,
            config_path=config,
            docker_compose=docker_compose,
            port=port,
            pull=pull,
            watch=watch,
            verbose=verbose,
            debugger_port=debugger_port,
            debugger_base_url=debugger_base_url,
            postgres_uri=postgres_uri,
        )
        # add up + options
        args.extend(["up", "--remove-orphans"])
        if recreate:
            args.extend(["--force-recreate", "--renew-anon-volumes"])
            try:
                runner.run(subp_exec("docker", "volume", "rm", "langgraph-data"))
            except click.exceptions.Exit:
                pass
        if watch:
            args.append("--watch")
        if wait:
            args.append("--wait")
        else:
            args.append("--abort-on-container-exit")
        # run docker compose
        set("Building...")

        def on_stdout(line: str):
            if "unpacking to docker.io" in line:
                set("Starting...")
            elif "Application startup complete" in line:
                debugger_origin = (
                    f"http://localhost:{debugger_port}"
                    if debugger_port
                    else "https://smith.langchain.com"
                )
                debugger_base_url_query = (
                    debugger_base_url or f"http://127.0.0.1:{port}"
                )
                set("")
                sys.stdout.write(
                    f"""Ready!
- API: http://localhost:{port}
- Docs: http://localhost:{port}/docs
- LangGraph Studio: {debugger_origin}/studio/?baseUrl={debugger_base_url_query}
"""
                )
                sys.stdout.flush()
                return True

        if capabilities.compose_type == "plugin":
            compose_cmd = ["docker", "compose"]
        elif capabilities.compose_type == "standalone":
            compose_cmd = ["docker-compose"]

        runner.run(
            subp_exec(
                *compose_cmd,
                *args,
                input=stdin,
                verbose=verbose,
                on_stdout=on_stdout,
            )
        )


def _build(
    runner,
    set: Callable[[str], None],
    config: pathlib.Path,
    config_json: dict,
    base_image: Optional[str],
    pull: bool,
    tag: str,
    passthrough: Sequence[str] = (),
):
    base_image = base_image or (
        "langchain/langgraphjs-api"
        if config_json.get("node_version")
        else "langchain/langgraph-api"
    )

    # pull latest images
    if pull:
        runner.run(
            subp_exec(
                "docker",
                "pull",
                f"{base_image}:{config_json['node_version']}"
                if config_json.get("node_version")
                else f"{base_image}:{config_json['python_version']}",
                verbose=True,
            )
        )
    set("Building...")
    # apply options
    args = [
        "-f",
        "-",  # stdin
        "-t",
        tag,
    ]
    # apply config
    stdin = langgraph_cli.config.config_to_docker(config, config_json, base_image)
    # run docker build
    runner.run(
        subp_exec(
            "docker",
            "build",
            *args,
            *passthrough,
            str(config.parent),
            input=stdin,
            verbose=True,
        )
    )


@OPT_CONFIG
@OPT_PULL
@click.option(
    "--tag",
    "-t",
    help="""Tag for the docker image.

    \b
    Example:
        langgraph build -t my-image

    \b
    """,
    required=True,
)
@click.option(
    "--base-image",
    hidden=True,
)
@click.argument("docker_build_args", nargs=-1, type=click.UNPROCESSED)
@cli.command(
    help="🛠️ Build LangGraph API server Docker image",
    context_settings=dict(
        ignore_unknown_options=True,
    ),
)
@log_command
def build(
    config: pathlib.Path,
    docker_build_args: Sequence[str],
    base_image: Optional[str],
    pull: bool,
    tag: str,
):
    with Runner() as runner, Progress(message="Pulling...") as set:
        if shutil.which("docker") is None:
            raise click.UsageError("Docker not installed") from None
        with open(config) as f:
            config_json = langgraph_cli.config.validate_config(json.load(f))
        _build(
            runner, set, config, config_json, base_image, pull, tag, docker_build_args
        )


def _get_docker_ignore_content() -> str:
    """Return the content of a .dockerignore file.

    This file is used to exclude files and directories from the Docker build context.

    It may be overly broad, but it's better to be safe than sorry.

    The main goal is to exclude .env files by default.
    """
    return """\
# Ignore node_modules and other dependency directories
node_modules
bower_components
vendor

# Ignore logs and temporary files
*.log
*.tmp
*.swp

# Ignore .env files and other environment files
.env
.env.*
*.local

# Ignore git-related files
.git
.gitignore

# Ignore Docker-related files and configs
.dockerignore
docker-compose.yml

# Ignore build and cache directories
dist
build
.cache
__pycache__

# Ignore IDE and editor configurations
.vscode
.idea
*.sublime-project
*.sublime-workspace
.DS_Store  # macOS-specific

# Ignore test and coverage files
coverage
*.coverage
*.test.js
*.spec.js
tests
"""


@OPT_CONFIG
@click.argument("save_path", type=click.Path(resolve_path=True))
@cli.command(
    help=(
        "🐳 Generate a Dockerfile for the LangGraph API server, "
        "with options for Docker Compose setup."
    )
)
@click.option(
    # Add a flag for adding a docker-compose.yml file as part of the output
    "--add-docker-compose",
    help=(
        "Add additional files for running the LangGraph API server with "
        "docker-compose. These files include a docker-compose.yml, .env file, "
        "and a .dockerignore file."
    ),
    is_flag=True,
)
@log_command
def dockerfile(save_path: str, config: pathlib.Path, add_docker_compose: bool) -> None:
    save_path = pathlib.Path(save_path).absolute()
    secho(f"🔍 Validating configuration at path: {config}", fg="yellow")
    with open(config, encoding="utf-8") as f:
        config_json = langgraph_cli.config.validate_config(json.load(f))
    secho("✅ Configuration validated!", fg="green")

    secho(f"📝 Generating Dockerfile at {save_path}", fg="yellow")
    with open(str(save_path), "w", encoding="utf-8") as f:
        f.write(
            langgraph_cli.config.config_to_docker(
                config,
                config_json,
                "langchain/langgraphjs-api"
                if config_json.get("node_version")
                else "langchain/langgraph-api",
            )
        )
    secho("✅ Added: Dockerfile", fg="green")

    if add_docker_compose:
        # Add docker compose and related files
        # Add .dockerignore file in the same directory as the Dockerfile
        with open(str(save_path.parent / ".dockerignore"), "w", encoding="utf-8") as f:
            f.write(_get_docker_ignore_content())
        secho("✅ Added: .dockerignore", fg="green")

        # Generate a docker-compose.yml file
        path = str(save_path.parent / "docker-compose.yml")
        with open(path, "w", encoding="utf-8") as f:
            with Runner() as runner:
                capabilities = langgraph_cli.docker.check_capabilities(runner)

            compose_dict = langgraph_cli.docker.compose_as_dict(
                capabilities,
                port=8123,
            )
            # Add .env file to the docker-compose.yml for the langgraph-api service
            compose_dict["services"]["langgraph-api"]["env_file"] = [".env"]
            # Add the Dockerfile to the build context
            compose_dict["services"]["langgraph-api"]["build"] = {
                "context": ".",
                "dockerfile": save_path.name,
            }
            f.write(langgraph_cli.docker.dict_to_yaml(compose_dict))
            secho("✅ Added: docker-compose.yml", fg="green")

        # Check if the .env file exists in the same directory as the Dockerfile
        if not (save_path.parent / ".env").exists():
            # Also add an empty .env file
            with open(str(save_path.parent / ".env"), "w", encoding="utf-8") as f:
                f.writelines(
                    [
                        "# Uncomment the following line to add your LangSmith API key",
                        "\n",
                        "# LANGSMITH_API_KEY=your-api-key",
                        "\n",
                        "# Or if you have a LangGraph Cloud license key, "
                        "then uncomment the following line: ",
                        "\n",
                        "# LANGGRAPH_CLOUD_LICENSE_KEY=your-license-key",
                        "\n",
                        "# Add any other environment variables go below...",
                    ]
                )

            secho("✅ Added: .env", fg="green")
        else:
            # Do nothing since the .env file already exists. Not a great
            # idea to overwrite in case the user has added custom env vars set
            # in the .env file already.
            secho("➖ Skipped: .env. It already exists!", fg="yellow")

    secho(
        f"🎉 Files generated successfully at path {save_path.parent}!",
        fg="cyan",
        bold=True,
    )


def prepare_args_and_stdin(
    *,
    capabilities: DockerCapabilities,
    config_path: pathlib.Path,
    config: Config,
    docker_compose: Optional[pathlib.Path],
    port: int,
    watch: bool,
    debugger_port: Optional[int] = None,
    debugger_base_url: Optional[str] = None,
    postgres_uri: Optional[str] = None,
):
    # prepare args
    stdin = langgraph_cli.docker.compose(
        capabilities,
        port=port,
        debugger_port=debugger_port,
        debugger_base_url=debugger_base_url,
        postgres_uri=postgres_uri,
    )
    args = [
        "--project-directory",
        str(config_path.parent),
    ]
    # apply options
    if docker_compose:
        args.extend(["-f", str(docker_compose)])
    args.extend(["-f", "-"])  # stdin
    # apply config
    stdin += langgraph_cli.config.config_to_compose(
        config_path,
        config,
        watch=watch,
        base_image="langchain/langgraphjs-api"
        if config.get("node_version")
        else "langchain/langgraph-api",
    )
    return args, stdin


def prepare(
    runner,
    *,
    capabilities: DockerCapabilities,
    config_path: pathlib.Path,
    docker_compose: Optional[pathlib.Path],
    port: int,
    pull: bool,
    watch: bool,
    verbose: bool,
    debugger_port: Optional[int] = None,
    debugger_base_url: Optional[str] = None,
    postgres_uri: Optional[str] = None,
):
    with open(config_path) as f:
        config = langgraph_cli.config.validate_config(json.load(f))
    # pull latest images
    if pull:
        runner.run(
            subp_exec(
                "docker",
                "pull",
                f"langchain/langgraphjs-api:{config['node_version']}"
                if config.get("node_version")
                else f"langchain/langgraph-api:{config['python_version']}",
                verbose=verbose,
            )
        )

    args, stdin = prepare_args_and_stdin(
        capabilities=capabilities,
        config_path=config_path,
        config=config,
        docker_compose=docker_compose,
        port=port,
        watch=watch,
        debugger_port=debugger_port,
        debugger_base_url=debugger_base_url or f"http://127.0.0.1:{port}",
        postgres_uri=postgres_uri,
    )
    return args, stdin
