{
  description = "remote-dev HostService test host artifacts";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
    disko.url = "github:nix-community/disko";
    disko.inputs.nixpkgs.follows = "nixpkgs";
  };

  outputs = { self, nixpkgs, flake-utils, disko }:
    let
      version = "0.2.4-host-service-test.20260518T165445Z-g08db1af299f4-dirty";

      hostArtifacts = {
        host_config_id = "remote-dev-nixos-host-v2";
        disko_layout_id = "single-disk-efi-ext4-v1";
        inherit version;
      };

      mkLocalBinaryPackage = pkgs: pname: binaryName: src:
        pkgs.stdenvNoCC.mkDerivation {
          inherit pname version src;
          sourceRoot = ".";
          dontUnpack = true;
          nativeBuildInputs = [ pkgs.autoPatchelfHook pkgs.gnutar pkgs.gzip ];
          buildInputs = [ pkgs.stdenv.cc.cc.lib ];
          installPhase = ''
            mkdir -p $out/bin
            tar xzf $src -C $out/bin
            chmod +x $out/bin/${binaryName}
          '';
        };

      mkTakeoverRunner = pkgs:
        pkgs.writeShellApplication {
          name = "remote-dev-takeover";
          runtimeInputs = [
            pkgs.bash
            pkgs.coreutils
            pkgs.curl
            pkgs.e2fsprogs
            pkgs.gawk
            pkgs.gnugrep
            pkgs.gnused
            pkgs.jq
            pkgs.nix
            pkgs.nixos-install-tools
            pkgs.systemd
            pkgs.util-linux
            disko.packages.${pkgs.stdenv.hostPlatform.system}.disko
          ];
          text = ''
            set -euo pipefail

            PLAN_FILE=/run/remote-dev/takeover-plan.json
            CONFIG_DIR=/run/remote-dev/target-config
            LOG_FILE=/run/remote-dev/takeover.log
            TARGET_DISK=""
            CURRENT_PHASE=bootstrap

            log() {
              printf 'remote-dev takeover: %s\n' "$*"
            }

            die() {
              printf 'remote-dev takeover: %s\n' "$*" >&2
              exit 1
            }

            phase() {
              CURRENT_PHASE="$1"
              label="''${2:-$CURRENT_PHASE}"
              log "phase: $label"
              if [ -s "$PLAN_FILE" ]; then
                post_event taking_over "$CURRENT_PHASE" "takeover phase: $label"
              fi
            }

            setup_logging() {
              mkdir -p /run/remote-dev
              exec > >(tee -a "$LOG_FILE") 2>&1
              log "logging to provider serial/system console and $LOG_FILE"
            }

            persist_takeover_log() {
              if mountpoint -q /mnt 2>/dev/null; then
                mkdir -p /mnt/var/log/remote-dev
                cp "$LOG_FILE" /mnt/var/log/remote-dev/takeover.log || true
              fi
            }

            report_failure() {
              status="$?"
              if [ "$status" -ne 0 ]; then
                detail="$(tail -n 40 "$LOG_FILE" 2>/dev/null | tr '\r\n' '  ' | cut -c1-480 || true)"
                [ -n "$detail" ] || detail="exit status: $status"
                log "FAILED during phase: $CURRENT_PHASE"
                log "exit status: $status"
                log "No automatic retry will run. Inspect the provider serial/system console for this log."
                log "If /mnt was already mounted, the latest log copy may also be at /mnt/var/log/remote-dev/takeover.log."
                if [ -s "$PLAN_FILE" ]; then
                  post_event failed "$CURRENT_PHASE" "takeover failed" "$detail"
                fi
              fi
            }

            field() {
              jq -r --arg key "$1" '.[$key] // ""' "$PLAN_FILE"
            }

            post_event() {
              state="$1"
              phase_name="$2"
              message="$3"
              last_error="''${4:-}"
              host_service_url="$(field host_service_url)"
              prepare_id="$(field prepare_id)"
              callback_token="$(field callback_token)"
              [ -n "$host_service_url" ] || return 0
              [ -n "$prepare_id" ] || return 0
              [ -n "$callback_token" ] || return 0
              if [ -n "$last_error" ]; then
                curl -fsS --connect-timeout 10 --max-time 30 -X POST \
                  -H "Authorization: Bearer $callback_token" \
                  --data-urlencode "state=$state" \
                  --data-urlencode "phase=$phase_name" \
                  --data-urlencode "message=$message" \
                  --data-urlencode "last_error=$last_error" \
                  "$host_service_url/v1/host-prepares/$prepare_id/events" >/dev/null || true
              else
                curl -fsS --connect-timeout 10 --max-time 30 -X POST \
                  -H "Authorization: Bearer $callback_token" \
                  --data-urlencode "state=$state" \
                  --data-urlencode "phase=$phase_name" \
                  --data-urlencode "message=$message" \
                  "$host_service_url/v1/host-prepares/$prepare_id/events" >/dev/null || true
              fi
            }

            nix_string() {
              jq -Rr @json
            }

            decode_plan() {
              mkdir -p /run/remote-dev
              plan_b64=""
              read -r -a cmdline_args < /proc/cmdline
              for arg in "''${cmdline_args[@]}"; do
                case "$arg" in
                  remote_dev_takeover_plan_b64=*) plan_b64="''${arg#remote_dev_takeover_plan_b64=}" ;;
                esac
              done
              [ -n "$plan_b64" ] || die "missing remote_dev_takeover_plan_b64 kernel parameter"
              printf '%s' "$plan_b64" | base64 -d > "$PLAN_FILE"
              jq -e . "$PLAN_FILE" >/dev/null
            }

            choose_target_disk() {
              disk_by_id="$(field target_disk_by_id)"
              disk_path="$(field target_disk)"
              if [ -n "$disk_by_id" ] && [ -b "$disk_by_id" ]; then
                TARGET_DISK="$(readlink -f "$disk_by_id")"
              else
                [ -n "$disk_path" ] || die "takeover plan did not include target_disk"
                [ -b "$disk_path" ] || die "target disk is not a block device: $disk_path"
                TARGET_DISK="$(readlink -f "$disk_path")"
              fi
              [ -b "$TARGET_DISK" ] || die "resolved target disk is not a block device: $TARGET_DISK"
            }

            disk_property() {
              disk="$1"
              key="$2"
              udevadm info --query=property --name "$disk" 2>/dev/null \
                | awk -F= -v key="$key" '$1 == key { print $2; exit }'
            }

            verify_disk_identity() {
              expected_size="$(field target_disk_size_bytes)"
              expected_serial="$(field target_disk_serial)"
              expected_wwn="$(field target_disk_wwn)"
              current_size="$(blockdev --getsize64 "$TARGET_DISK")"
              [ -n "$expected_size" ] || die "takeover plan did not include target disk size"
              [ "$current_size" = "$expected_size" ] || die "target disk size changed: expected $expected_size, got $current_size"

              if [ -n "$expected_serial" ]; then
                current_serial="$(disk_property "$TARGET_DISK" ID_SERIAL)"
                [ -n "$current_serial" ] || current_serial="$(lsblk -dnro SERIAL "$TARGET_DISK" | awk 'NF { print; exit }')"
                [ "$current_serial" = "$expected_serial" ] || die "target disk serial changed"
              fi

              if [ -n "$expected_wwn" ]; then
                current_wwn="$(disk_property "$TARGET_DISK" ID_WWN)"
                [ -n "$current_wwn" ] || current_wwn="$(lsblk -dnro WWN "$TARGET_DISK" | awk 'NF { print; exit }')"
                [ "$current_wwn" = "$expected_wwn" ] || die "target disk WWN changed"
              fi
            }

            run_nix() {
              nix --extra-experimental-features 'nix-command flakes' "$@"
            }

            write_target_flake() {
              mkdir -p "$CONFIG_DIR"
              remote_dev_bin_ref="$(field remote_dev_bin_ref)"
              [ -n "$remote_dev_bin_ref" ] || die "takeover plan did not include remote_dev_bin_ref"
              cat > "$CONFIG_DIR/flake.nix" <<REMOTE_DEV_FLAKE
            {
              inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
              inputs.flake-utils.url = "github:numtide/flake-utils";
              inputs.disko.url = "github:nix-community/disko";
              inputs.disko.inputs.nixpkgs.follows = "nixpkgs";
              inputs.remote-dev-bin.url = "$remote_dev_bin_ref";
              inputs.remote-dev-bin.inputs.nixpkgs.follows = "nixpkgs";
              inputs.remote-dev-bin.inputs.flake-utils.follows = "flake-utils";

              outputs = { nixpkgs, flake-utils, remote-dev-bin, disko, ... }:
                {
                  nixosConfigurations.remote-dev-host = nixpkgs.lib.nixosSystem {
                    system = "x86_64-linux";
                    modules = [
                      remote-dev-bin.nixosModules.remote-dev-host
                      remote-dev-bin.nixosModules.single-disk-efi-ext4
                      ./hardware-configuration.nix
                      ./configuration.nix
                    ];
                  };
                } // flake-utils.lib.eachDefaultSystem (system: {
                  apps.disko = {
                    type = "app";
                    program = "\''${disko.packages.\''${system}.disko}/bin/disko";
                  };
                });
            }
            REMOTE_DEV_FLAKE
              cat > "$CONFIG_DIR/hardware-configuration.nix" <<'REMOTE_DEV_EMPTY_HARDWARE'
            { ... }:

            {
              # Disk filesystems are owned by the remote-dev disko module. Do not
              # generate fileSystems here, or nixos-install will see duplicate /
              # and /boot definitions after disko formats the target.
            }
            REMOTE_DEV_EMPTY_HARDWARE
            }

            write_registration_files() {
              mkdir -p /mnt/etc/remote-dev
              field host_service_url > /mnt/etc/remote-dev/host-service-url
              field prepare_id > /mnt/etc/remote-dev/firstboot-prepare-id
              field callback_token > /mnt/etc/remote-dev/firstboot-callback-token
              field bootstrap_public_ip > /mnt/etc/remote-dev/bootstrap-public-ip
              chmod 600 /mnt/etc/remote-dev/firstboot-callback-token
              cat > /mnt/etc/remote-dev/firstboot-register.sh <<'REMOTE_DEV_FIRSTBOOT'
            #!/usr/bin/env bash
            set -euo pipefail
            mkdir -p /var/log/remote-dev
            exec > >(tee -a /var/log/remote-dev/firstboot-register.log) 2>&1
            marker=/var/lib/remote-dev/firstboot-register.done
            response=/var/lib/remote-dev/firstboot-register-response.json
            current_phase=firstboot_register
            post_event() {
              state="$1"
              phase="$2"
              message="$3"
              last_error="''${4:-}"
              base_url="$(cat /etc/remote-dev/host-service-url)"
              prepare_id="$(cat /etc/remote-dev/firstboot-prepare-id)"
              callback_token="$(cat /etc/remote-dev/firstboot-callback-token)"
              if [ -n "$last_error" ]; then
                curl -fsS --connect-timeout 10 --max-time 30 -X POST \
                  -H "Authorization: Bearer $callback_token" \
                  --data-urlencode "state=$state" \
                  --data-urlencode "phase=$phase" \
                  --data-urlencode "message=$message" \
                  --data-urlencode "last_error=$last_error" \
                  "$base_url/v1/host-prepares/$prepare_id/events" >/dev/null || true
              else
                curl -fsS --connect-timeout 10 --max-time 30 -X POST \
                  -H "Authorization: Bearer $callback_token" \
                  --data-urlencode "state=$state" \
                  --data-urlencode "phase=$phase" \
                  --data-urlencode "message=$message" \
                  "$base_url/v1/host-prepares/$prepare_id/events" >/dev/null || true
              fi
            }
            report_failure() {
              status="$?"
              if [ "$status" -ne 0 ]; then
                post_event firstboot "$current_phase" "firstboot registration failed; retrying" "exit status: $status"
              fi
            }
            trap report_failure EXIT
            [ -e "$marker" ] && exit 0
            mkdir -p /var/lib/remote-dev
            echo "remote-dev firstboot register: starting"
            base_url="$(cat /etc/remote-dev/host-service-url)"
            prepare_id="$(cat /etc/remote-dev/firstboot-prepare-id)"
            callback_token="$(cat /etc/remote-dev/firstboot-callback-token)"
            fallback_ip="$(cat /etc/remote-dev/bootstrap-public-ip)"
            post_event firstboot firstboot_register "firstboot registration started"
            public_ip="$(curl -fsS --connect-timeout 5 --max-time 15 https://api.ipify.org || true)"
            [ -n "$public_ip" ] || public_ip="$fallback_ip"
            [ -n "$public_ip" ] || { echo "could not detect public IP for HostService management target" >&2; exit 1; }
            management_target="root@$public_ip"
            echo "remote-dev firstboot register: management_target=$management_target"
            tmp="$(mktemp)"
            curl -fsS --connect-timeout 10 --max-time 60 \
              -X POST \
              -H "Authorization: Bearer $callback_token" \
              --data-urlencode "management_target=$management_target" \
              "$base_url/v1/host-prepares/$prepare_id/firstboot" > "$tmp"
            mv "$tmp" "$response"
            touch "$marker"
            post_event ready firstboot_register "firstboot registration completed"
            trap - EXIT
            echo "remote-dev firstboot register: completed"
            REMOTE_DEV_FIRSTBOOT
              chmod 700 /mnt/etc/remote-dev/firstboot-register.sh
            }

            write_configuration() {
              disk_nix="$(printf '%s' "$TARGET_DISK" | nix_string)"
              management_public_key_nix="$(field management_public_key | nix_string)"
              cat > "$CONFIG_DIR/configuration.nix" <<REMOTE_DEV_NIXOS
            { pkgs, ... }:

            {
              remote-dev.hostArtifacts.singleDiskEfiExt4.disk = $disk_nix;
              networking.hostName = "remote-dev-host";
              networking.networkmanager.enable = true;
              nix.settings.experimental-features = [ "nix-command" "flakes" ];
              users.users.root.openssh.authorizedKeys.keys = [ $management_public_key_nix ];

              systemd.services.remote-dev-firstboot-register = {
                description = "Register this host with remote-dev HostService";
                wantedBy = [ "multi-user.target" ];
                wants = [ "network-online.target" ];
                after = [ "network-online.target" "sshd.service" ];
                path = [ pkgs.bash pkgs.coreutils pkgs.curl pkgs.jq ];
                unitConfig.StartLimitIntervalSec = 0;
                serviceConfig = {
                  Type = "oneshot";
                  Restart = "on-failure";
                  RestartSec = "30s";
                };
                script = "exec /etc/remote-dev/firstboot-register.sh";
              };
            }
            REMOTE_DEV_NIXOS
            }

            apply_disk_layout() {
              run_nix run "$CONFIG_DIR#disko" -- --mode disko --flake "$CONFIG_DIR#remote-dev-host"
            }

            copy_nixos_config() {
              mkdir -p /mnt/etc/nixos
              cp "$CONFIG_DIR/flake.nix" "$CONFIG_DIR/flake.lock" "$CONFIG_DIR/configuration.nix" "$CONFIG_DIR/hardware-configuration.nix" /mnt/etc/nixos/
            }

            write_authorized_keys() {
              mkdir -p /mnt/root/.ssh
              field management_public_key > /mnt/root/.ssh/authorized_keys
              chmod 700 /mnt/root/.ssh
              chmod 600 /mnt/root/.ssh/authorized_keys
            }

            write_install_proof() {
              install_id="install-$(date -u +%Y%m%dT%H%M%SZ)-$(cat /proc/sys/kernel/random/uuid)"
              lock_hash="sha256-$(sha256sum "$CONFIG_DIR/flake.lock" | awk '{print $1}')"
              jq -n \
                --arg host_config_id "$(field host_config_id)" \
                --arg flake_lock_hash "$lock_hash" \
                --arg disko_layout_id "$(field disko_layout_id)" \
                --arg current_system "x86_64-linux" \
                --arg hostctrl_version "$(field hostctrl_version)" \
                --arg remote_dev_bin_ref "$(field remote_dev_bin_ref)" \
                --arg install_id "$install_id" \
                '{
                  host_config_id: $host_config_id,
                  flake_lock_hash: $flake_lock_hash,
                  disko_layout_id: $disko_layout_id,
                  current_system: $current_system,
                  hostctrl_version: $hostctrl_version,
                  remote_dev_bin_ref: $remote_dev_bin_ref,
                  install_id: $install_id,
                  install_source: "installer",
                  clean_install: true
                }' > /mnt/etc/remote-dev/host-install-proof.json
            }

            install_nixos() {
              nixos-install --root /mnt --flake "$CONFIG_DIR#remote-dev-host" --no-root-passwd
            }

            main() {
              setup_logging
              trap report_failure EXIT
              phase decode_takeover_plan "decode takeover plan"
              decode_plan
              phase choose_target_disk "choose target disk"
              choose_target_disk
              phase verify_target_disk_identity "verify target disk identity"
              verify_disk_identity
              log "verified target disk $TARGET_DISK; starting destructive install"
              phase write_target_flake "write target flake"
              write_target_flake
              phase write_target_configuration "write target configuration"
              write_configuration
              phase lock_target_flake "lock target flake"
              run_nix flake lock "$CONFIG_DIR"
              phase apply_disk_layout "apply disk layout"
              apply_disk_layout
              phase copy_nixos_configuration "copy NixOS configuration"
              copy_nixos_config
              phase install_nixos "install NixOS"
              install_nixos
              phase write_management_authorized_keys "write management authorized_keys"
              write_authorized_keys
              phase write_firstboot_registration_files "write firstboot registration files"
              write_registration_files
              phase write_install_proof "write install proof"
              write_install_proof
              phase persist_takeover_log "persist takeover log"
              log "install complete; rebooting into remote-dev NixOS host"
              persist_takeover_log
              trap - EXIT
              systemctl reboot
            }

            main "$@"
          '';
        };

      remoteDevKexecSystem = nixpkgs.lib.nixosSystem {
        system = "x86_64-linux";
        modules = [
          ({ modulesPath, pkgs, ... }:
            let
              takeoverRunner = mkTakeoverRunner pkgs;
            in
            {
              imports = [ (modulesPath + "/installer/netboot/netboot-minimal.nix") ];

              boot.kernelParams = [
                "console=tty0"
                "console=ttyS0,115200n8"
                "remote_dev_takeover=1"
              ];
              boot.zfs.forceImportRoot = false;

              nix.settings.experimental-features = [ "nix-command" "flakes" ];

              environment.systemPackages = [
                pkgs.curl
                pkgs.jq
                pkgs.nix
                pkgs.nixos-install-tools
                disko.packages.${"x86_64-linux"}.disko
                takeoverRunner
              ];

              systemd.services.remote-dev-takeover = {
                description = "remote-dev HostService destructive takeover";
                wantedBy = [ "multi-user.target" ];
                wants = [ "network-online.target" ];
                after = [ "network-online.target" ];
                serviceConfig = {
                  Type = "oneshot";
                  StandardOutput = "journal+console";
                  StandardError = "journal+console";
                };
                script = "exec ${takeoverRunner}/bin/remote-dev-takeover";
              };

              system.stateVersion = "25.11";
            })
        ];
      };

      mkKexecInstallerPackage = pkgs:
        pkgs.runCommand "remote-dev-kexec-installer-${version}" { } ''
          mkdir -p $out
          ln -s ${remoteDevKexecSystem.config.system.build.kexecTree}/bzImage $out/bzImage
          ln -s ${remoteDevKexecSystem.config.system.build.kexecTree}/initrd.gz $out/initrd.gz
          ln -s ${remoteDevKexecSystem.config.system.build.kexecTree}/kexec-boot $out/kexec-boot
          cat > $out/remote-dev-kexec-runner <<'REMOTE_DEV_KEXEC_RUNNER'
          #!/usr/bin/env bash
          set -euo pipefail
          if [ "$#" -ne 1 ]; then
            echo "usage: remote-dev-kexec-runner <takeover-plan-base64>" >&2
            exit 2
          fi
          plan_b64="$1"
          script_dir="$(cd -- "$(dirname -- "''${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
          command_line="init=${remoteDevKexecSystem.config.system.build.toplevel}/init ${toString remoteDevKexecSystem.config.boot.kernelParams} remote_dev_takeover_plan_b64=$plan_b64"
          kexec --load "$script_dir/bzImage" \
            --initrd="$script_dir/initrd.gz" \
            --command-line "$command_line"
          sync
          systemctl kexec 2>/dev/null || kexec -e
          REMOTE_DEV_KEXEC_RUNNER
          chmod +x $out/remote-dev-kexec-runner
        '';
    in
    {
      lib.hostArtifacts = hostArtifacts;
      nixosConfigurations.remote-dev-kexec-installer = remoteDevKexecSystem;

      nixosModules.remote-dev-host = { pkgs, ... }:
        let
          system = pkgs.stdenv.hostPlatform.system;
          hostctrlPackage = self.packages.${system}.remote-dev-hostctrl or
            (throw "remote-dev-hostctrl test artifact is only published for x86_64-linux");
        in
        {
          services.openssh.enable = true;
          services.openssh.settings.PasswordAuthentication = false;
          services.openssh.settings.KbdInteractiveAuthentication = false;
          services.openssh.settings.PermitRootLogin = "prohibit-password";

          networking.firewall.allowedUDPPortRanges = [
            { from = 60000; to = 61000; }
          ];

          users.groups."remote-dev-lease" = {};
          nix.settings.trusted-users = [ "root" "@remote-dev-lease" ];
          security.sudo.extraConfig = ''
            %remote-dev-lease ALL=(ALL:ALL) NOPASSWD:SETENV: ALL
          '';

          environment.systemPackages = [
            pkgs.bash
            pkgs.coreutils
            pkgs.curl
            pkgs.git
            pkgs.iproute2
            pkgs.mosh
            pkgs.openssh
            pkgs.systemd
            hostctrlPackage
          ];

          environment.etc."remote-dev/host-artifacts.json".text =
            builtins.toJSON hostArtifacts;

          systemd.tmpfiles.rules = [
            "d /var/lib/remote-dev 0755 root root - -"
            "d /var/lib/remote-dev/leases 0711 root root - -"
            "d /var/lib/remote-dev/templates 0755 root root - -"
            "d /var/lib/remote-dev/templates/nspawn-v1 0755 root root - -"
          ];

          system.stateVersion = "25.11";
        };

      nixosModules.single-disk-efi-ext4 = { config, lib, ... }:
        let
          cfg = config.remote-dev.hostArtifacts.singleDiskEfiExt4;
        in
        {
          imports = [ disko.nixosModules.disko ];

          options.remote-dev.hostArtifacts.singleDiskEfiExt4.disk = lib.mkOption {
            type = lib.types.str;
            description = "Block device that will be wiped for the remote-dev HostPool baseline.";
          };

          config = {
            boot.loader.systemd-boot.enable = true;
            boot.loader.efi.canTouchEfiVariables = false;

            disko.devices.disk.remote-dev-root = {
              type = "disk";
              device = cfg.disk;
              content = {
                type = "gpt";
                partitions = {
                  ESP = {
                    priority = 1;
                    size = "512M";
                    type = "EF00";
                    content = {
                      type = "filesystem";
                      format = "vfat";
                      mountpoint = "/boot";
                      mountOptions = [ "umask=0077" ];
                    };
                  };
                  root = {
                    size = "100%";
                    content = {
                      type = "filesystem";
                      format = "ext4";
                      mountpoint = "/";
                    };
                  };
                };
              };
            };
          };
        };
    } //
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = import nixpkgs { inherit system; };
        lib = nixpkgs.lib;
      in
      {
        packages = lib.optionalAttrs (system == "x86_64-linux") rec {
          remote-dev-hostctrl = mkLocalBinaryPackage
            pkgs
            "remote-dev-hostctrl"
            "remote-dev-hostctrl"
            ./remote-dev-hostctrl-x86_64-linux.tar.gz;
          remote-dev-kexec-installer = mkKexecInstallerPackage pkgs;
          default = remote-dev-hostctrl;
        };
      });
}
