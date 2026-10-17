.PHONY: test world corpus notebook clean

test:          ## run the offline unit tests
	pytest -q

world:         ## (re)generate the synthetic world fixtures
	python -m lab.world

corpus:        ## build and write the dev/test corpus split
	python -m lab.corpus

notebook:      ## run the study notebook headlessly (needs Ollama + model)
	MPLBACKEND=Agg jupyter nbconvert --to notebook --execute \
	  --ExecutePreprocessor.timeout=3600 \
	  --output attack_lab.ipynb notebooks/attack_lab.ipynb

clean:         ## remove generated traces
	rm -rf results/traces && mkdir -p results/traces
