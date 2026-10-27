// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     https://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

// Package {{cookiecutter.agent_package}} contains the root agent implementation.
package {{cookiecutter.agent_package}}

import (
	"context"

	"google.golang.org/genai"

	"google.golang.org/adk/v2/agent"
	"google.golang.org/adk/v2/agent/llmagent"
	"google.golang.org/adk/v2/model/gemini"
	"google.golang.org/adk/v2/tool"
	"google.golang.org/adk/v2/tool/functiontool"
)

// modelName is the Gemini model used by the root agent.
const modelName = "{{cookiecutter.default_model}}"

// GetWeatherArgs defines the input for the get_weather tool.
type GetWeatherArgs struct {
	Query string `json:"query" jsonschema:"The location to get weather information for"`
}

// GetWeatherResult defines the output for the get_weather tool.
type GetWeatherResult struct {
	Weather string `json:"weather"`
}

// GetWeather returns mock weather data for a location.
// It is a sample tool tested in agent_test.go; update that file if you change it.
func GetWeather(_ agent.Context, args GetWeatherArgs) (GetWeatherResult, error) {
	return GetWeatherResult{
		Weather: "It's sunny and 72°F in " + args.Query,
	}, nil
}

// GetCurrentTimeArgs defines the input for the get_current_time tool.
type GetCurrentTimeArgs struct {
	Query string `json:"query" jsonschema:"The location to get the current time for"`
}

// GetCurrentTimeResult defines the output for the get_current_time tool.
type GetCurrentTimeResult struct {
	Time string `json:"time"`
}

// GetCurrentTime returns mock time data for a location.
func GetCurrentTime(_ agent.Context, args GetCurrentTimeArgs) (GetCurrentTimeResult, error) {
	return GetCurrentTimeResult{
		Time: "It's 3:00 PM in " + args.Query,
	}, nil
}

// NewRootAgent creates and returns the root agent with all configured tools.
func NewRootAgent(ctx context.Context) (agent.Agent, error) {
	model, err := gemini.NewModel(ctx, modelName, &genai.ClientConfig{
		Backend: genai.BackendVertexAI,
	})
	if err != nil {
		return nil, err
	}

	weatherTool, err := functiontool.New(functiontool.Config{
		Name:        "get_weather",
		Description: "Get the current weather for a city.",
	}, GetWeather)
	if err != nil {
		return nil, err
	}

	timeTool, err := functiontool.New(functiontool.Config{
		Name:        "get_current_time",
		Description: "Get the current time for a city.",
	}, GetCurrentTime)
	if err != nil {
		return nil, err
	}

	rootAgent, err := llmagent.New(llmagent.Config{
		// Keep in sync with agents-cli-manifest.yaml: agents-cli derives this name
		// from the project `name:` recorded there, and telemetry reports it as
		// gen_ai.agent.name. Renaming the agent only here makes the two disagree,
		// and anything selecting traces by name stops finding this agent's.
		Name:        "{{cookiecutter.root_agent_name}}",
		Model:       model,
		Description: "A helpful AI assistant.",
		Instruction: "You are a helpful AI assistant designed to provide accurate and useful information.",
		Tools:       []tool.Tool{weatherTool, timeTool},
	})
	if err != nil {
		return nil, err
	}

	return rootAgent, nil
}
