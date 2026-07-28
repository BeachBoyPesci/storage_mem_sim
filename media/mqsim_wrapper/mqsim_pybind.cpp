/**
 * pybind11 bindings for MQSim SSD simulator.
 *
 * Wraps MQSim's core simulation (from MQSim/src/main.cpp) into a
 * Python-callable function.  All MQSim source lives under MQSim/ —
 * this file is the only glue and lives outside the submodule.
 *
 * Build via CMake (CMakeLists.txt in this directory), then:
 *
 *     import _mqsim
 *     _mqsim.run(ssd_config_path, workload_config_path, output_dir)
 *     stats = _mqsim.run_with_stats(ssd_config_path, workload_config_path, output_dir)
 */

#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

#include <iostream>
#include <fstream>
#include <sstream>
#include <ctime>
#include <string>
#include <cstring>
#include <vector>

// MQSim headers — all under MQSim/src/
#include "ssd/SSD_Defs.h"
#include "exec/Execution_Parameter_Set.h"
#include "exec/SSD_Device.h"
#include "exec/Host_System.h"
#include "utils/rapidxml/rapidxml.hpp"
#include "utils/DistributionTypes.h"
#include "utils/Workload_Statistics.h"
#include "sim/Engine.h"

namespace py = pybind11;

// ---------------------------------------------------------------------------
// Helper: read SSD configuration XML  (adapted from main.cpp)
// ---------------------------------------------------------------------------

static Execution_Parameter_Set* read_config(const std::string& path) {
    auto* params = new Execution_Parameter_Set;
    std::ifstream file(path);
    if (!file) {
        Utils::XmlWriter w;
        w.Open(path.c_str());
        params->XML_serialize(w);
        w.Close();
        return params;
    }
    std::string line((std::istreambuf_iterator<char>(file)),
                     std::istreambuf_iterator<char>());
    file.close();
    if (line == "USE_INTERNAL_PARAMS") {
        return params;
    }
    rapidxml::xml_document<> doc;
    char* buf = new char[line.length() + 1];
    std::strcpy(buf, line.c_str());
    doc.parse<0>(buf);
    auto* node = doc.first_node("Execution_Parameter_Set");
    if (node) {
        params->XML_deserialize(node);
    }
    delete[] buf;
    return params;
}

// ---------------------------------------------------------------------------
// Helper: read workload definitions XML  (adapted from main.cpp)
// ---------------------------------------------------------------------------

static std::vector<std::vector<IO_Flow_Parameter_Set*>*>*
read_workloads(const std::string& path) {
    auto* scenarios = new std::vector<std::vector<IO_Flow_Parameter_Set*>*>;
    std::ifstream file(path);
    if (!file) {
        return scenarios;
    }
    std::string line((std::istreambuf_iterator<char>(file)),
                     std::istreambuf_iterator<char>());
    file.close();
    if (line == "USE_INTERNAL_PARAMS") {
        return scenarios;
    }
    rapidxml::xml_document<> doc;
    char* buf = new char[line.length() + 1];
    std::strcpy(buf, line.c_str());
    doc.parse<0>(buf);
    auto* root = doc.first_node("MQSim_IO_Scenarios");
    if (root) {
        for (auto* scn = root->first_node("IO_Scenario"); scn;
             scn = scn->next_sibling("IO_Scenario")) {
            auto* def = new std::vector<IO_Flow_Parameter_Set*>;
            for (auto* flow = scn->first_node(); flow;
                 flow = flow->next_sibling()) {
                IO_Flow_Parameter_Set* f = nullptr;
                if (std::strcmp(flow->name(),
                                "IO_Flow_Parameter_Set_Synthetic") == 0) {
                    auto* sf = new IO_Flow_Parameter_Set_Synthetic;
                    sf->XML_deserialize(flow);
                    f = sf;
                } else if (std::strcmp(flow->name(),
                                       "IO_Flow_Parameter_Set_Trace_Based") == 0) {
                    auto* tf = new IO_Flow_Parameter_Set_Trace_Based;
                    tf->XML_deserialize(flow);
                    f = tf;
                }
                if (f) def->push_back(f);
            }
            scenarios->push_back(def);
        }
    }
    delete[] buf;
    return scenarios;
}

// ---------------------------------------------------------------------------
// Helper: write results XML  (identical to main.cpp collect_results)
// ---------------------------------------------------------------------------

static void write_results(SSD_Device& ssd, Host_System& host,
                          const std::string& path) {
    Utils::XmlWriter w;
    w.Open(path.c_str());
    w.Write_open_tag(std::string("MQSim_Results"));
    host.Report_results_in_XML("", w);
    ssd.Report_results_in_XML("", w);
    w.Write_close_tag();
    w.Close();
}

// ---------------------------------------------------------------------------
// Helper: print flow statistics  (matches main.cpp collect_results stdout)
// ---------------------------------------------------------------------------

static void print_flow_stats(Host_System& host) {
    std::vector<Host_Components::IO_Flow_Base*> IO_flows = host.Get_io_flows();
    for (unsigned int stream_id = 0; stream_id < IO_flows.size(); stream_id++) {
        std::cout << "Flow " << IO_flows[stream_id]->ID()
                  << " - total requests generated: "
                  << IO_flows[stream_id]->Get_generated_request_count()
                  << " total requests serviced: "
                  << IO_flows[stream_id]->Get_serviced_request_count()
                  << std::endl;
        std::cout << "                   - device response time: "
                  << IO_flows[stream_id]->Get_device_response_time() << " (us)"
                  << " end-to-end request delay: "
                  << IO_flows[stream_id]->Get_end_to_end_request_delay()
                  << " (us)" << std::endl;
    }
}

// ---------------------------------------------------------------------------
// Helper: extract stats into a dict for Python consumption
// ---------------------------------------------------------------------------

static py::dict collect_flow_stats(Host_System& host, SSD_Device& ssd) {
    py::dict result;
    std::vector<Host_Components::IO_Flow_Base*> IO_flows = host.Get_io_flows();

    if (!IO_flows.empty()) {
        auto* flow = IO_flows[0];
        result["generated_request_count"] =
            (unsigned long long)flow->Get_generated_request_count();
        result["serviced_request_count"] =
            (unsigned long long)flow->Get_serviced_request_count();
        result["device_response_time_us"] =
            (unsigned long long)flow->Get_device_response_time();
        result["end_to_end_request_delay_us"] =
            (unsigned long long)flow->Get_end_to_end_request_delay();
    }
    return result;
}

// ---------------------------------------------------------------------------
// Core simulation (shared by run() and run_with_stats())
// ---------------------------------------------------------------------------

static bool simulate(const std::string& ssd_config_path,
                     const std::string& workload_config_path,
                     const std::string& output_dir,
                     py::dict* out_stats) {

    // Suppress MQSim's verbose cout during simulation
    std::stringstream sink;
    std::streambuf* old_cout = std::cout.rdbuf(sink.rdbuf());

    bool ok = false;
    Execution_Parameter_Set* exec_params = nullptr;
    std::vector<std::vector<IO_Flow_Parameter_Set*>*>* io_scenarios = nullptr;

    try {
        exec_params = read_config(ssd_config_path);
        io_scenarios = read_workloads(workload_config_path);

        int cntr = 1;
        for (auto it = io_scenarios->begin(); it != io_scenarios->end();
             ++it, ++cntr) {

            Simulator->Reset();

            exec_params->Host_Configuration.IO_Flow_Definitions.clear();
            for (auto* flow : **it) {
                exec_params->Host_Configuration.IO_Flow_Definitions
                    .push_back(flow);
            }

            SSD_Device ssd(&exec_params->SSD_Device_Configuration,
                           &exec_params->Host_Configuration.IO_Flow_Definitions);

            std::string base = workload_config_path.substr(
                0, workload_config_path.find_last_of("."));
            exec_params->Host_Configuration.Input_file_path = base;

            Host_System host(&exec_params->Host_Configuration,
                             exec_params->SSD_Device_Configuration
                                 .Enabled_Preconditioning,
                             ssd.Host_interface);
            host.Attach_ssd_device(&ssd);

            Simulator->Start_simulation();

            // Write results XML  (same output as MQSim standalone binary)
            std::string out = output_dir + "/workload_scenario_"
                            + std::to_string(cntr) + ".xml";
            write_results(ssd, host, out);

            // Restore cout momentarily to print flow statistics
            std::cout.rdbuf(old_cout);
            print_flow_stats(host);
            old_cout = std::cout.rdbuf(sink.rdbuf());

            // Capture stats for Python if requested
            if (out_stats != nullptr) {
                *out_stats = collect_flow_stats(host, ssd);
            }
        }

        ok = true;
    } catch (const std::exception& e) {
        std::cerr << "[_mqsim] simulation error: " << e.what() << std::endl;
        ok = false;
    } catch (...) {
        std::cerr << "[_mqsim] unknown simulation error" << std::endl;
        ok = false;
    }

    // Cleanup
    std::cout.rdbuf(old_cout);

    if (exec_params != nullptr) {
        delete exec_params;
    }
    if (io_scenarios != nullptr) {
        for (auto* scenario : *io_scenarios) {
            for (auto* flow : *scenario) {
                delete flow;
            }
            delete scenario;
        }
        delete io_scenarios;
    }

    return ok;
}

// ---------------------------------------------------------------------------
// Python-callable: run() — returns bool   (backward compatible)
// ---------------------------------------------------------------------------

bool run(const std::string& ssd_config_path,
         const std::string& workload_config_path,
         const std::string& output_dir = ".") {
    return simulate(ssd_config_path, workload_config_path, output_dir, nullptr);
}

// ---------------------------------------------------------------------------
// Python-callable: run_with_stats() — returns dict with flow statistics
// ---------------------------------------------------------------------------

py::dict run_with_stats(const std::string& ssd_config_path,
                         const std::string& workload_config_path,
                         const std::string& output_dir = ".") {
    py::dict stats;
    if (!simulate(ssd_config_path, workload_config_path, output_dir, &stats)) {
        throw std::runtime_error("MQSim simulation failed");
    }
    return stats;
}

// ---------------------------------------------------------------------------
// pybind11 module
// ---------------------------------------------------------------------------

PYBIND11_MODULE(_mqsim, m) {
    m.doc() = "MQSim SSD simulator — native pybind11 bindings";
    m.def("run", &run,
          py::arg("ssd_config_path"),
          py::arg("workload_config_path"),
          py::arg("output_dir") = ".",
          "Run MQSim simulation.  Writes workload_scenario_N.xml to "
          "output_dir.  Returns True on success.");

    m.def("run_with_stats", &run_with_stats,
          py::arg("ssd_config_path"),
          py::arg("workload_config_path"),
          py::arg("output_dir") = ".",
          "Run MQSim simulation and return a dict with key flow statistics "
          "(generated_request_count, serviced_request_count, "
          "device_response_time_us, end_to_end_request_delay_us).  "
          "Also writes workload_scenario_N.xml to output_dir.");
}
