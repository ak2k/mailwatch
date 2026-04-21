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
#
# Wave 4 will flesh this out. Wave 0 leaves it as a skeleton.

{ config, lib, pkgs, mailwatchPackage }:

with lib;

let
  cfg = config.services.mailwatch;
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
  };

  # Wave 4 will populate config = mkIf cfg.enable { ... } with
  # systemd.services.mailwatch, users.users.mailwatch, nginx virtualHost,
  # systemd.tmpfiles, etc. Kept empty at Wave 0 so the module eval'es
  # without pulling in service definitions that depend on other modules
  # not yet written.
  config = mkIf cfg.enable {
    users.users.${cfg.user} = {
      isSystemUser = true;
      group = cfg.group;
      home = cfg.stateDir;
      createHome = false;
    };
    users.groups.${cfg.group} = { };

    systemd.tmpfiles.rules = [
      "d ${cfg.stateDir} 0770 ${cfg.user} ${cfg.group} -"
    ];
  };
}
