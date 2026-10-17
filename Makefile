.PHONY: test world corpus clean

test:          ## run the offline unit tests
	pytest -q

world:         ## (re)generate the synthetic world fixtures
	python -m lab.world

corpus:        ## build and write the dev/test corpus split
	python -m lab.corpus

clean:         ## remove generated traces
	rm -rf results/traces && mkdir -p results/traces
