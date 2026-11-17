package app

import (
	"example.test/cachefixture/changed"
	"example.test/cachefixture/stable"
)

func Value() int { return stable.Value() + changed.Value() }
