{
  lib,
  buildPythonPackage,
  uv-build,
  pytest,
}:
buildPythonPackage {
  pname = "cpi";
  version = "0.1.0";
  pyproject = true;
  src = ./.;

  build-system = [ uv-build ];

  # No runtime dependencies on purpose: CPI is standard library only, which is what
  # makes it cheap to import from anywhere.
  dependencies = [ ];

  nativeCheckInputs = [ pytest ];
  checkPhase = ''
    runHook preCheck
    # The real-export tests skip unless CPI_TEST_{PIPEPIPE,GADGETBRIDGE,FIREFOX}_EXPORT
    # points somewhere, which none of them ever do in a sandbox.
    pytest -q
    runHook postCheck
  '';

  pythonImportsCheck = [
    "cuso4d"
    "cuso4d.pipepipe"
    "cuso4d.gadgetbridge"
    "cuso4d.firefox"
  ];

  meta = {
    description = "Typed access layer over personal data exports";
    license = lib.licenses.cc0;
  };
}
