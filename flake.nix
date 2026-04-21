{
  description = "mailwatch — self-hosted USPS IMb letter tracker";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";
    pyproject-nix = {
      url = "github:pyproject-nix/pyproject.nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    uv2nix = {
      url = "github:pyproject-nix/uv2nix";
      inputs = {
        pyproject-nix.follows = "pyproject-nix";
        nixpkgs.follows = "nixpkgs";
      };
    };
    pyproject-build-systems = {
      url = "github:pyproject-nix/build-system-pkgs";
      inputs = {
        pyproject-nix.follows = "pyproject-nix";
        uv2nix.follows = "uv2nix";
        nixpkgs.follows = "nixpkgs";
      };
    };
  };

  outputs =
    {
      self,
      nixpkgs,
      pyproject-nix,
      uv2nix,
      pyproject-build-systems,
      ...
    }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
        "x86_64-darwin"
        "aarch64-darwin"
      ];
      forAllSystems = nixpkgs.lib.genAttrs systems;

      mkPythonSet =
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          workspace = uv2nix.lib.workspace.loadWorkspace { workspaceRoot = ./.; };
          overlay = workspace.mkPyprojectOverlay { sourcePreference = "wheel"; };
        in
        {
          inherit workspace;
          pythonSet =
            (pkgs.callPackage pyproject-nix.build.packages {
              python = pkgs.python312;
            }).overrideScope
              (
                nixpkgs.lib.composeManyExtensions [
                  pyproject-build-systems.overlays.wheel
                  overlay
                ]
              );
        };
    in
    {
      packages = forAllSystems (
        system:
        let
          s = mkPythonSet system;
        in
        {
          default = s.pythonSet.mkVirtualEnv "mailwatch-env" s.workspace.deps.default;
          mailwatch = s.pythonSet.mkVirtualEnv "mailwatch-env" s.workspace.deps.default;
          mailwatch-dev = s.pythonSet.mkVirtualEnv "mailwatch-env-dev" s.workspace.deps.all;
        }
      );

      checks = forAllSystems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          s = mkPythonSet system;
          devEnv = s.pythonSet.mkVirtualEnv "mailwatch-env-dev" s.workspace.deps.all;
        in
        {
          # Python tooling caches must point at the sandbox-writable $TMPDIR;
          # the source tree `cd`-d into is a read-only /nix/store path.
          tests = pkgs.runCommand "mailwatch-tests" { nativeBuildInputs = [ devEnv ]; } ''
            cp -r ${./.}/. .
            chmod -R u+w .
            export HOME=$TMPDIR
            export PYTEST_CACHE_DIR=$TMPDIR/.pytest_cache
            pytest
            touch $out
          '';
          lint = pkgs.runCommand "mailwatch-lint" { nativeBuildInputs = [ devEnv ]; } ''
            cp -r ${./.}/. .
            chmod -R u+w .
            export RUFF_CACHE_DIR=$TMPDIR/.ruff_cache
            ruff check .
            ruff format --check .
            touch $out
          '';
          typecheck = pkgs.runCommand "mailwatch-typecheck" { nativeBuildInputs = [ devEnv ]; } ''
            cp -r ${./.}/. .
            chmod -R u+w .
            export MYPY_CACHE_DIR=$TMPDIR/.mypy_cache
            mypy mailwatch
            touch $out
          '';
        }
      );

      devShells = forAllSystems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          s = mkPythonSet system;
          devEnv = s.pythonSet.mkVirtualEnv "mailwatch-env-dev" s.workspace.deps.all;
        in
        {
          default = pkgs.mkShell {
            packages = [
              devEnv
              pkgs.uv
            ];
            shellHook = ''
              export UV_NO_SYNC=1
              export UV_PYTHON="${devEnv}/bin/python"
            '';
          };
        }
      );

      nixosModules.mailwatch =
        {
          config,
          lib,
          pkgs,
          ...
        }:
        import ./nix/module.nix {
          inherit config lib pkgs;
          mailwatchPackage = self.packages.${pkgs.stdenv.hostPlatform.system}.mailwatch;
        };

      nixosModules.default = self.nixosModules.mailwatch;
    };
}
