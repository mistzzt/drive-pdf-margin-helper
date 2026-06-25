# Not in nixpkgs; vendored here so the flake stays self-contained.
{
  lib,
  buildPythonApplication,
  fetchPypi,
  setuptools,
  pillow,
  pymupdf,
  ghostscript,
  poppler-utils,
}:
buildPythonApplication {
  pname = "pdfCropMargins";
  version = "2.2.1";
  pyproject = true;

  src = fetchPypi {
    # PyPI normalizes the sdist filename to all-lowercase.
    pname = "pdfcropmargins";
    version = "2.2.1";
    hash = "sha256-FjOFw61fGXB2M8LadUEm0pKn2vUl1SkgBsxOmBrXtIQ=";
  };

  build-system = [setuptools];

  dependencies = [pillow pymupdf];

  # ghostscript / pdftoppm are invoked at runtime for bounding-box detection.
  makeWrapperArgs = [
    "--prefix PATH : ${lib.makeBinPath [ghostscript poppler-utils]}"
  ];

  pythonImportsCheck = ["pdfCropMargins"];

  meta = {
    description = "Crop the margins of a PDF file by adjusting the CropBox (no re-render, no bloat)";
    homepage = "https://github.com/abarker/pdfCropMargins";
    license = lib.licenses.gpl3Plus;
    mainProgram = "pdfcropmargins";
  };
}
