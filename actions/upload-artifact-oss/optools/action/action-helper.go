package main

import (
	"errors"
	"os"
	"strings"

	"github.com/guguducken/upload-artifact-oss/pkg/config"
	"github.com/guguducken/upload-artifact-oss/pkg/github"
	"github.com/guguducken/upload-artifact-oss/pkg/oss"
	"gopkg.in/yaml.v3"
)

// WorkflowInputs allow you to specify data that the action expects to use during runtime.
// GitHub stores input parameters as environment variables.
var WorkflowInputs = github.Inputs{
	config.EnvOSSProvider: github.Input{
		Name:        config.EnvOSSProvider,
		Description: "oss provider used to upload and store artifacts, currently supports " + strings.Join(oss.SupportedProviders, ", "),
		Required:    true,
		Default:     oss.CosProvider,
	},
	config.EnvOSSAccessKey: github.Input{
		Name:        config.EnvOSSAccessKey,
		Description: "access key used to access oss service",
		Required:    true,
		Default:     "",
	},
	config.EnvOSSSecretKey: github.Input{
		Name:        config.EnvOSSSecretKey,
		Description: "secret key used to access oss service",
		Required:    true,
		Default:     "",
	},
	config.EnvOSSRegion: github.Input{
		Name:        config.EnvOSSRegion,
		Description: "the region where the oss service is located",
		Required:    true,
		Default:     "",
	},
	config.EnvOSSBucket: github.Input{
		Name:        config.EnvOSSBucket,
		Description: "the bucket used to store uploaded artifacts in oss",
		Required:    true,
		Default:     "",
	},
	config.EnvOSSStorePath: github.Input{
		Name:        config.EnvOSSStorePath,
		Description: "The path where the artifact is stored in the bucket",
		Required:    true,
		Default:     "",
	},
	config.EnvOSSStorageClass: github.Input{
		Name:        config.EnvOSSStorageClass,
		Description: "The storage type when the artifact is stored in the bucket",
		Required:    true,
		Default:     "",
	},
	config.EnvArtifactPath: github.Input{
		Name:        config.EnvArtifactPath,
		Description: "The path used to obtain the artifact",
		Required:    true,
		Default:     "",
	},
	config.EnvRetryTimes: github.Input{
		Name:        config.EnvRetryTimes,
		Description: "The number of retries that can be made when uploading artifacts，default is 1",
		Required:    false,
		Default:     "1",
	},
}

// WorkflowOutputs allow you to declare data that an action sets.
// Actions that run later in a workflow can use the output data set in previously run actions.
var WorkflowOutputs = github.Outputs{}

var (
	// Name action name, setup by makefile
	Name = ""
	// Author action author, setup by makefile
	Author = ""
	// Description action description, setup by makefile
	Description = ""
	// Icon action icon, setup by makefile
	Icon = ""
	// Color action icon color, setup by makefile
	Color = ""

	// Image the docker image to use as the container to run the action, this will override default value(Dockerfile)
	Image = ""
	// Args an array of strings that define the inputs for a Docker container, need setup there
	Args = []string{}
	// Env specifies a key/value map of environment variables to set in the container environment
	Env = map[string]string{}
	// Entrypoint overrides the Docker ENTRYPOINT in the Dockerfile, or sets it if one wasn't already specified
	Entrypoint = ""
	// PreEntrypoint allows you to run a script before the entrypoint action begins
	PreEntrypoint = ""
	// PostEntrypoint allows you to run a cleanup script once the runs.entrypoint action has completed
	PostEntrypoint = ""
)

type Action struct {
	Name        string         `yaml:"name"`
	Author      string         `yaml:"author"`
	Description string         `yaml:"description"`
	Inputs      github.Inputs  `yaml:"inputs,omitempty"`
	Outputs     github.Outputs `yaml:"outputs,omitempty"`
	Runs        Runs           `yaml:"runs"`
	Branding    *Branding      `yaml:"branding,omitempty"`
}

type Runs struct {
	Using          string            `yaml:"using"`
	Image          string            `yaml:"image,omitempty"`
	Args           []string          `yaml:"args,omitempty"`
	Env            map[string]string `yaml:"env,omitempty"`
	Entrypoint     string            `yaml:"entrypoint,omitempty"`
	PreEntrypoint  string            `yaml:"pre_entrypoint,omitempty"`
	PostEntrypoint string            `yaml:"post_entrypoint,omitempty"`
}

type Branding struct {
	Icon  string `yaml:"icon"`
	Color string `yaml:"color"`
}

func main() {
	action := GenDefaultDockerAction(Name, Author, Description)
	action.Inputs = WorkflowInputs
	action.Outputs = WorkflowOutputs

	if Image != "" {
		action.Runs.Image = Image
	}

	// set customer settings
	action.Runs.Args = Args
	action.Runs.Env = Env
	action.Runs.Entrypoint = Entrypoint
	action.Runs.PreEntrypoint = PreEntrypoint
	action.Runs.PostEntrypoint = PostEntrypoint

	action.Branding = &Branding{
		Icon:  Icon,
		Color: Color,
	}

	if err := action.Validate(); err != nil {
		panic(err)
	}

	data, err := yaml.Marshal(action)
	if err != nil {
		panic(err)
	}
	err = os.WriteFile("action.yaml", data, 0644)
	if err != nil {
		panic(err)
	}
}

func GenDefaultDockerAction(name, author, description string) Action {
	return Action{
		Name:        name,
		Author:      author,
		Description: description,
		Runs: Runs{
			Using: "docker",
			Image: "Dockerfile",
		},
	}
}

func (a Action) Validate() error {
	if a.Name == "" {
		return errors.New("action name is required")
	}
	if a.Description == "" {
		return errors.New("action description is required")
	}

	// check action inputs
	if len(a.Inputs) != 0 {
		for key, value := range a.Inputs {
			if value.Name != key {
				return errors.New("action input's name must equal to key")
			}
			if value.Description == "" {
				return errors.New("action input's description is required")
			}

			if err := InputKeyCheck(value.Name); err != nil {
				return err
			}
		}
	}

	// check action outputs
	if len(a.Outputs) != 0 {
		for key, value := range a.Outputs {
			if err := InputKeyCheck(key); err != nil {
				return err
			}
			if value.Description == "" {
				return errors.New("action output's description is required")
			}
		}
	}

	if a.Runs.Using != "docker" {
		return errors.New("action runs using must set value to 'docker'")
	}

	if a.Runs.Image != "Dockerfile" {
		if !strings.HasPrefix(a.Runs.Image, "docker://") {
			return errors.New("action runs image must start with 'docker://' or use default value(Dockerfile)")
		}
	}
	return nil
}

func InputKeyCheck(key string) error {
	if !(key[0] != '_' || 'a' <= key[0] && key[0] <= 'z' || 'A' <= key[0] && key[0] <= 'Z') {
		return errors.New("invalid input key, which must start with a letter or _")
	}
	for _, char := range key {
		if !(char == '_' || char == '-' || 'a' <= char && char <= 'z' || 'A' <= char && char <= 'Z' || '0' <= char && char <= '9') {
			return errors.New("invalid character in key, which contain only alphanumeric characters, -, or _")
		}
	}
	return nil
}
