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
1. Clone the repository: `https://github.com/TJ690/SWE-Group-Project--Project-3`
2. Create the virtual environment:
  - python -m venv .venv
  - .venv\Scripts\Activate.ps1
3. Install dependencies: pip install -r requirements.txt

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
