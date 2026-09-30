# SWE-Group-Project--Project-3 - GitSkills Authorship Signal Detection

## Overview
This project aims to investigate whether we can distinguish likely human-authored, agent-assisted, and highly templated skills by observing structural, linguistic, and commit-metadata patterns in GitSkills artifacts. This is a class project for SWE-380/CSC-580, inspired by the MSR 2027 Mining Challenge, focusing on research question #6. Results are framed as heuristic signals and not confirmed authorship claims - See THREATS_TO_VALIDITY.md.

## Team
 - Product Owner
 - Scrum Master
 - Developers/Researchers

## Repository Structure
- src/ - pipeline code
- tests/ - unit tests
- data/ data acquisition instructions and small samples (see data/README.md)
- results/ - generated output tables
- figures/ - generated plots
- report/ - research report drafts
- docs/decisions/ - decision log
- docs/meeting-notes/ - sprint meeting notes
- ai-use-log.md - AI usage disclosure

## Setup
Use Python 3.13. Commands below are run from the repository root.

1. Clone the repository:
   ```bash
   git clone https://github.com/TJ690/SWE-Group-Project--Project-3
   cd SWE-Group-Project--Project-3
   ```
2. Create and activate a virtual environment.

   macOS and Linux:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```

   Windows (PowerShell):
   ```powershell
   python -m venv .venv
   .venv\Scripts\Activate.ps1
   ```
   The shell prompt shows `(.venv)` while the environment is active. Leave it with `deactivate`.
3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
4. Point Jupyter at this environment before opening notebooks in `src/notebooks/`:
   ```bash
   python -m ipykernel install --user --name gitskills --display-name "GitSkills (.venv)"
   ```
   Select the **GitSkills (.venv)** kernel in each notebook.

## Dataset Acquisition
This project uses a sample of the GitSkills dataset (Hugging Face: mvaccargiu/gitskills). See data/README.md for download instructions. Only commit the sample in data/samples/.

## Running the Pipeline
To be added once the pipeline is implemented

## Outputs
To be added

## Current Status
Pre-sprint: repository skeleton, finalized research question, pulled initial data sample

## Limitations
See THREATS_TO_VALIDITY.md for full report. Key limitation: There is no way to be sure of the authorship label for GitSkills artifacts, so all classifications are heuristic signals, not facts. 
