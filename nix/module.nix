# NixOS module for mailwatch.
#
# This is the public, generic service module. Parameterised via options —
# downstream consumers (including private infrastructure repos) set
# domain, secrets path, bucket, etc. This module must have zero
# references to specific deployments.
#
# Example usage in a host config:
#
#   services.mailwatch = {
#     enable = true;
#     domain = "mail.example.com";
#     environmentFile = "/run/secrets/mailwatch.env";
#   };

{
  config,
  lib,
  pkgs,
  mailwatchPackage,
}:

let
  inherit (lib)
    mkIf
    mkOption
    mkEnableOption
    types
    literalExpression
    concatStringsSep
    ;

  cfg = config.services.mailwatch;

  dbPath = "${cfg.stateDir}/mailwatch.db";

  # Shared systemd hardening bundle. Applied to every mailwatch service
  # (app, poll, cleanup). Based on the nixpkgs lego/acme Go-service
  # pattern — @system-service + @resources for the runtime, deny
  # @privileged, then re-allow @chown (Go's os.Chown and unix-socket
  # creation path on aarch64 uses fchownat, which is otherwise killed by
  # ~@privileged). Safe default for Python services too; if Litestream
  # is wired in downstream as a sidecar it needs the @chown re-add for
  # the same reason.
  hardening = {
    ProtectSystem = "strict";
    ProtectHome = true;
    PrivateTmp = true;
    PrivateDevices = true;
    NoNewPrivileges = true;
    ProtectKernelTunables = true;
    ProtectKernelModules = true;
    ProtectKernelLogs = true;
    ProtectControlGroups = true;
    ProtectProc = "invisible";
    ProcSubset = "pid";
    ProtectHostname = true;
    ProtectClock = true;
    RestrictNamespaces = true;
    RestrictAddressFamilies = [
      "AF_UNIX"
      "AF_INET"
      "AF_INET6"
    ];
    RestrictRealtime = true;
    RestrictSUIDSGID = true;
    LockPersonality = true;
    CapabilityBoundingSet = "";
    AmbientCapabilities = "";
    SystemCallArchitectures = "native";
    SystemCallFilter = [
      "@system-service"
      "@resources"
      "~@privileged"
      "@chown"
    ];
  };
in
{
  options.services.mailwatch = {
    enable = mkEnableOption "mailwatch — USPS IMb letter tracker";

    package = mkOption {
      type = types.package;
      default = mailwatchPackage;
      defaultText = literalExpression "mailwatch.packages.<system>.mailwatch";
      description = "The mailwatch Python environment to run.";
    };

    user = mkOption {
      type = types.str;
      default = "mailwatch";
      description = "System user running the service.";
    };

    group = mkOption {
      type = types.str;
      default = "mailwatch";
      description = "System group for the service.";
    };

    stateDir = mkOption {
      type = types.path;
      default = "/var/lib/mailwatch";
      description = "Directory for the SQLite DB and runtime state.";
    };

    bindAddress = mkOption {
      type = types.str;
      default = "127.0.0.1";
      description = "Address gunicorn binds to. Leave at 127.0.0.1 if fronted by a reverse proxy on the same host.";
    };

    bindPort = mkOption {
      type = types.port;
      default = 8082;
      description = "TCP port gunicorn binds to.";
    };

    workers = mkOption {
      type = types.ints.positive;
      default = 2;
      description = ''
        Number of gunicorn worker processes. Each runs its own
        lifespan (including the USPS OAuth refresh loop), so more workers
        multiply outbound auth traffic. 2 gives HA without waste.
      '';
    };

    environmentFile = mkOption {
      type = types.path;
      description = ''
        Path to an environment file supplying secrets (MAILER_ID,
        USPS_NEWAPI_CUSTOMER_ID/_SECRET, BSG_USERNAME/_PASSWORD, SESSION_KEY,
        etc). See .env.example in the mailwatch repo for the full list.
        Typically rendered by sops-nix, agenix, or similar.
      '';
    };

    domain = mkOption {
      type = types.nullOr types.str;
      default = null;
      description = ''
        Public-facing hostname. If set, enables an nginx virtualhost
        proxying to bindAddress:bindPort with ACME TLS. Leave null to run
        bare and handle reverse-proxy configuration externally.
      '';
    };

    poll = {
      enable = mkEnableOption "the IV-MTR pull-poll daemon";

      interval = mkOption {
        type = types.str;
        default = "30min";
        description = ''
          How often to trigger the poll. Passed to the timer as
          OnUnitActiveSec, so systemd.time(7) syntax applies
          (e.g. "30min", "1h", "6h").
        '';
      };

      lookbackDays = mkOption {
        type = types.ints.positive;
        default = 14;
        description = "How many days back to request in each IV-MTR pull.";
      };
    };

    cleanup = {
      enable = mkOption {
        type = types.bool;
        default = true;
        description = "Run a periodic cleanup job to prune old scan events and expired sessions.";
      };

      schedule = mkOption {
        type = types.str;
        default = "daily";
        description = ''
          systemd OnCalendar expression controlling when the cleanup
          job runs (e.g. "daily", "hourly", "Mon *-*-* 03:00:00").
        '';
      };
    };
  };

  config = mkIf cfg.enable {
    users.users.${cfg.user} = {
      isSystemUser = true;
      inherit (cfg) group;
      home = cfg.stateDir;
      createHome = false;
      # isSystemUser already blocks interactive login; no explicit shell needed.
    };
    users.groups.${cfg.group} = { };

    systemd = {
      tmpfiles.rules = [
        "d ${cfg.stateDir} 0770 ${cfg.user} ${cfg.group} -"
      ];

      services = {
        # Main web service (gunicorn + UvicornWorker).
        mailwatch = {
          description = "mailwatch — USPS IMb letter tracker (web)";
          after = [ "network.target" ];
          wantedBy = [ "multi-user.target" ];
          environment = {
            DB_PATH = dbPath;
          };
          serviceConfig = hardening // {
            Type = "notify";
            NotifyAccess = "main";
            KillMode = "mixed";
            KillSignal = "SIGTERM";
            TimeoutStopSec = 30;
            User = cfg.user;
            Group = cfg.group;
            StateDirectory = "mailwatch";
            StateDirectoryMode = "0770";
            RuntimeDirectory = "mailwatch";
            EnvironmentFile = cfg.environmentFile;
            UMask = "0007";
            ReadWritePaths = [ cfg.stateDir ];
            ExecStart = concatStringsSep " " [
              "${cfg.package}/bin/gunicorn"
              "--workers ${toString cfg.workers}"
              "--worker-class uvicorn.workers.UvicornWorker"
              "--bind ${cfg.bindAddress}:${toString cfg.bindPort}"
              "--forwarded-allow-ips 127.0.0.1"
              "--graceful-timeout 30"
              "--timeout 60"
              "--access-logfile -"
              "--error-logfile -"
              # Factory syntax: gunicorn calls `create_app()` once per worker
              # after import. Avoids eager module-level app creation that
              # would force env-var validation at pytest collection time.
              "mailwatch.app:create_app()"
            ];
            ExecReload = "${pkgs.coreutils}/bin/kill -HUP $MAINPID";
            Restart = "on-failure";
            RestartSec = 5;
          };
        };

        # IV-MTR pull poller (optional).
        mailwatch-poll = mkIf cfg.poll.enable {
          description = "mailwatch — IV-MTR pull poll";
          after = [
            "network.target"
            "mailwatch.service"
          ];
          environment = {
            DB_PATH = dbPath;
            POLL_LOOKBACK_DAYS = toString cfg.poll.lookbackDays;
          };
          serviceConfig = hardening // {
            Type = "oneshot";
            User = cfg.user;
            Group = cfg.group;
            StateDirectory = "mailwatch";
            StateDirectoryMode = "0770";
            EnvironmentFile = cfg.environmentFile;
            UMask = "0007";
            ReadWritePaths = [ cfg.stateDir ];
            ExecStart = "${cfg.package}/bin/python -m mailwatch.poll";
          };
        };

        # Periodic cleanup (optional, default on).
        mailwatch-cleanup = mkIf cfg.cleanup.enable {
          description = "mailwatch — prune old scan events and expired sessions";
          environment = {
            DB_PATH = dbPath;
          };
          serviceConfig = hardening // {
            Type = "oneshot";
            User = cfg.user;
            Group = cfg.group;
            StateDirectory = "mailwatch";
            StateDirectoryMode = "0770";
            EnvironmentFile = cfg.environmentFile;
            UMask = "0007";
            ReadWritePaths = [ cfg.stateDir ];
            ExecStart = "${cfg.package}/bin/python -m mailwatch.cleanup";
          };
        };
      };

      timers = {
        mailwatch-poll = mkIf cfg.poll.enable {
          description = "mailwatch — trigger IV-MTR poll";
          wantedBy = [ "timers.target" ];
          timerConfig = {
            OnBootSec = "5min";
            OnUnitActiveSec = cfg.poll.interval;
            Persistent = true;
            Unit = "mailwatch-poll.service";
          };
        };

        mailwatch-cleanup = mkIf cfg.cleanup.enable {
          description = "mailwatch — run cleanup on a schedule";
          wantedBy = [ "timers.target" ];
          timerConfig = {
            OnCalendar = cfg.cleanup.schedule;
            Persistent = true;
            RandomizedDelaySec = "30min";
            Unit = "mailwatch-cleanup.service";
          };
        };
      };
    };

    # --- Optional nginx vhost ---
    # When cfg.domain is set, expose the app over TLS via nginx with
    # ACME. No auth policy is baked in — downstream consumers layer
    # Cloudflare Access, HTTP basic auth, or oauth2-proxy as needed.
    services.nginx = mkIf (cfg.domain != null) {
      enable = true;
      recommendedProxySettings = true;
      recommendedTlsSettings = true;
      virtualHosts.${cfg.domain} = {
        enableACME = true;
        forceSSL = true;
        locations."/" = {
          proxyPass = "http://${cfg.bindAddress}:${toString cfg.bindPort}";
          proxyWebsockets = true;
          extraConfig = ''
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
            proxy_set_header X-Forwarded-Host $host;
          '';
        };
      };
    };
  };
}
