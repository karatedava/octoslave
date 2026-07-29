# Homebrew formula for OctoSlave.
#
# Use via a tap:
#
#   brew tap karatedava/tap
#   brew install octoslave
#
# Replace `url` and `sha256` for each release. Extra Python dependencies are
# resolved by Homebrew's `Language::Python::Virtualenv` against PyPI.
#
class Octoslave < Formula
  include Language::Python::Virtualenv

  desc "Autonomous AI research & coding assistant (CLI + web UI)"
  homepage "https://octoslave.karamazov.website"
  url "https://files.pythonhosted.org/packages/source/o/octoslave/octoslave-0.2.0.tar.gz"
  sha256 "0000000000000000000000000000000000000000000000000000000000000000"
  license "MIT"
  head "https://github.com/karatedava/octoslave.git", branch: "main"

  depends_on "python@3.12"

  # Top-level resources are resolved at brew-bump time via:
  #   brew update-python-resources octoslave
  # The list below is a starter; CI auto-refreshes it on tag.
  resource "openai" do
    url "https://files.pythonhosted.org/packages/source/o/openai/openai-1.50.0.tar.gz"
    sha256 "0000000000000000000000000000000000000000000000000000000000000000"
  end

  resource "rich" do
    url "https://files.pythonhosted.org/packages/source/r/rich/rich-13.7.1.tar.gz"
    sha256 "0000000000000000000000000000000000000000000000000000000000000000"
  end

  resource "click" do
    url "https://files.pythonhosted.org/packages/source/c/click/click-8.1.7.tar.gz"
    sha256 "0000000000000000000000000000000000000000000000000000000000000000"
  end

  resource "prompt_toolkit" do
    url "https://files.pythonhosted.org/packages/source/p/prompt_toolkit/prompt_toolkit-3.0.43.tar.gz"
    sha256 "0000000000000000000000000000000000000000000000000000000000000000"
  end

  resource "fastapi" do
    url "https://files.pythonhosted.org/packages/source/f/fastapi/fastapi-0.110.0.tar.gz"
    sha256 "0000000000000000000000000000000000000000000000000000000000000000"
  end

  resource "uvicorn" do
    url "https://files.pythonhosted.org/packages/source/u/uvicorn/uvicorn-0.27.0.tar.gz"
    sha256 "0000000000000000000000000000000000000000000000000000000000000000"
  end

  def install
    virtualenv_install_with_resources
  end

  test do
    assert_match "OctoSlave", shell_output("#{bin}/ots --help")
    # Version-agnostic: assert a semver is printed, not a pinned number (which
    # would rot on every release). The concrete version comes from `url` above.
    assert_match(/\d+\.\d+\.\d+/, shell_output("#{bin}/ots --version 2>/dev/null || true"))
  end
end
