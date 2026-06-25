{
  description = "scribe-crop: auto-crop PDF margins from a OneDrive folder for the Kindle Scribe";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";
    systems.url = "github:nix-systems/default";
  };

  outputs = {
    self,
    nixpkgs,
    systems,
  }: let
    forAllSystems = nixpkgs.lib.genAttrs (import systems);

    pkgsFor = system: nixpkgs.legacyPackages.${system};

    pdfcropmarginsFor = pkgs:
      pkgs.python314Packages.callPackage ./nix/pdfcropmargins.nix {};

    scribeCropFor = pkgs: let
      pdfcropmargins = pdfcropmarginsFor pkgs;
    in
      pkgs.python314Packages.buildPythonApplication {
        pname = "scribe-crop";
        version = "0.1.0";
        pyproject = true;

        src = ./.;

        build-system = [pkgs.python314Packages.hatchling];

        dependencies = [pkgs.python314Packages.watchdog];

        # gs is added explicitly: the service probes `gs --version` itself, and
        # the pdfcropmargins wrapper only exposes gs to its own children.
        makeWrapperArgs = [
          "--prefix PATH : ${pkgs.lib.makeBinPath [pdfcropmargins pkgs.ghostscript]}"
        ];

        # tests run via uv (need extra PATH/sandbox setup)
        doCheck = false;
        pythonImportsCheck = ["scribe_crop"];

        meta = {
          description = "Auto-crop PDF margins from a OneDrive folder for the Kindle Scribe";
          mainProgram = "scribe-crop";
          license = pkgs.lib.licenses.mit;
        };
      };

    module = import ./nix/module.nix;
  in {
    packages = forAllSystems (system: let
      pkgs = pkgsFor system;
      scribe-crop = scribeCropFor pkgs;
    in {
      inherit scribe-crop;
      pdfcropmargins = pdfcropmarginsFor pkgs;
      default = scribe-crop;
    });

    overlays.default = final: prev: {
      scribe-crop = scribeCropFor final;
    };

    nixosModules.scribe-crop = {pkgs, ...}: {
      imports = [module];
      services.scribe-crop.package = nixpkgs.lib.mkDefault (scribeCropFor pkgs);
    };
    nixosModules.default = self.nixosModules.scribe-crop;

    devShells = forAllSystems (system: let
      pkgs = pkgsFor system;
    in {
      default = pkgs.mkShell {
        packages = [
          pkgs.python314
          pkgs.uv
          (pdfcropmarginsFor pkgs)
        ];
      };
    });

    checks = forAllSystems (system: let
      pkgs = pkgsFor system;
    in {
      scribe-crop = self.packages.${system}.scribe-crop;

      # Exercise module eval + generated unit without a full NixOS toplevel.
      module-eval = let
        evaluated = nixpkgs.lib.evalModules {
          modules = [
            {_module.args = {inherit pkgs;};}
            {
              options.systemd.services = nixpkgs.lib.mkOption {
                type = nixpkgs.lib.types.attrs;
                default = {};
              };
            }
            self.nixosModules.default
            {
              services.scribe-crop = {
                enable = true;
                user = "scribe";
                root = "/home/scribe/OneDrive/ScribeCrop";
                readinessMarker = "/run/onedrive-ready/mirror-ready";
                settings = {
                  worker_count = 1;
                };
              };
            }
          ];
        };
        unit = evaluated.config.systemd.services.scribe-crop;
        execStart = unit.serviceConfig.ExecStart;
        inherit (nixpkgs.lib) hasInfix;
      in
        # The readiness-marker flag gates the destructive reverse-GC; assert it
        # renders, and that --mirror-current is absent when not requested.
        assert hasInfix "--readiness-marker /run/onedrive-ready/mirror-ready" execStart;
        assert !(hasInfix "--mirror-current" execStart);
        assert unit.serviceConfig.User == "scribe";
        assert unit.serviceConfig.Restart == "on-failure";
          pkgs.runCommand "scribe-crop-module-eval" {} ''
            test -n "${execStart}"
            touch $out
          '';
    });
  };
}
