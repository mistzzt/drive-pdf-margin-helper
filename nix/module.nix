{
  config,
  lib,
  pkgs,
  ...
}: let
  cfg = config.services.scribe-crop;

  tomlFormat = pkgs.formats.toml {};

  stateDir = "/var/lib/scribe-crop";

  serverSettings =
    {
      inherit (cfg) root;
      # State store lives in the provisioned StateDirectory, outside the synced
      # subtree, so it never round-trips to the cloud.
      state_path = "${stateDir}/state.db";
    }
    // cfg.settings;

  serverToml = tomlFormat.generate "scribe-crop-server.toml" serverSettings;
in {
  options.services.scribe-crop = {
    enable = lib.mkEnableOption "the scribe-crop PDF margin cropping service";

    package = lib.mkOption {
      type = lib.types.package;
      description = "The scribe-crop package to run.";
    };

    user = lib.mkOption {
      type = lib.types.str;
      description = ''
        User to run the service as. Must own the local OneDrive mirror so it can
        read upload/ and write processed/ and failed/.
      '';
    };

    group = lib.mkOption {
      type = lib.types.str;
      default = "users";
      description = "Group to run the service as.";
    };

    root = lib.mkOption {
      type = lib.types.str;
      example = "/home/alice/OneDrive/ScribeCrop";
      description = ''
        Local OneDrive mirror subtree containing config.toml and the
        upload/processed/failed dirs. The onedrive client keeps this in sync.
      '';
    };

    settings = lib.mkOption {
      type = tomlFormat.type;
      default = {};
      example = {
        stability_seconds = 5.0;
        worker_count = 1;
        retry_backoff.max_attempts = 8;
      };
      description = ''
        Server config fields rendered to the server TOML (root is set
        separately). See the project README for the full schema.
      '';
    };

    mirrorCurrent = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = ''
        Pass --mirror-current, telling the service the local mirror is always
        current enough to run the destructive reverse-GC pass. Only safe when the
        mirror is guaranteed synced before the service starts; otherwise use
        readinessMarker.
      '';
    };

    readinessMarker = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      example = "/run/onedrive-ready/mirror-ready";
      description = ''
        Path whose existence signals the mirror is current enough to GC. Should
        live OUTSIDE the synced subtree (e.g. under /run) so it never round-trips
        to the cloud. The onedrive integration is expected to create it once an
        initial sync completes.

        Do NOT place it under this service's own RuntimeDirectory
        (/run/scribe-crop): that directory is wiped and recreated empty on every
        scribe-crop stop/restart (RuntimeDirectoryPreserve defaults to no, and
        Restart=on-failure makes restarts routine), which would silently delete
        the marker and disable reverse-GC. Use a path owned by the sync-readiness
        signal instead.
      '';
    };

    onedriveUnit = lib.mkOption {
      type = lib.types.str;
      default = "onedrive.service";
      description = ''
        Name of the onedrive systemd unit to order after. The operator configures
        services.onedrive separately; this service only orders behind it.
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    systemd.services.scribe-crop = {
      description = "scribe-crop PDF margin cropping service";
      wantedBy = ["multi-user.target"];
      after = ["network.target" cfg.onedriveUnit];
      wants = [cfg.onedriveUnit];

      path = [cfg.package];

      serviceConfig = {
        ExecStart = let
          flags =
            lib.optional cfg.mirrorCurrent "--mirror-current"
            ++ lib.optionals (cfg.readinessMarker != null) [
              "--readiness-marker"
              cfg.readinessMarker
            ];
        in
          lib.escapeShellArgs (
            ["${cfg.package}/bin/scribe-crop" "-c" "${serverToml}" "run"] ++ flags
          );
        User = cfg.user;
        Group = cfg.group;
        Restart = "on-failure";
        RestartSec = "10s";
        StateDirectory = "scribe-crop";
        RuntimeDirectory = "scribe-crop";
      };
    };
  };
}
