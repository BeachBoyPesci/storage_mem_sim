"""MQSim workload XML generation.

Simple template-based approach: reads a workload XML template, replaces
<File_Path> with the actual trace path, and writes the result.
"""

import os
import re

_DEFAULT_TEMPLATE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))),
    "configs",
    "mqsim",
    "default_workload.xml",
)


def generate_workload_xml(trace_path, output_path, template_path=None):
    """Generate a workload XML by replacing File_Path in a template.

    Args:
        trace_path: Absolute path to the MQSim trace file.
        output_path: Where to write the generated workload XML.
        template_path: Optional custom template. Falls back to
                       configs/mqsim/default_workload.xml.

    Returns:
        The *output_path*.

    Raises:
        FileNotFoundError: If the template file does not exist.
    """
    tpl = template_path or _DEFAULT_TEMPLATE

    if not os.path.isfile(tpl):
        raise FileNotFoundError(f"Workload template not found: {tpl}")

    with open(tpl, "r", encoding="utf-8") as fh:
        xml = fh.read()

    xml = re.sub(
        r"<File_Path>.*?</File_Path>",
        lambda _: f"<File_Path>{trace_path}</File_Path>",
        xml,
        count=1,
    )

    out_dir = os.path.dirname(output_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    with open(output_path, "w", encoding="utf-8") as fh:
        fh.write(xml)

    return output_path


# ------------------------------------------------------------------
# Backward-compatible class wrapper
# ------------------------------------------------------------------

class MQSimWorkload:
    """Backward-compatible workload generator."""

    @classmethod
    def default(cls):
        return cls()

    def build_trace_based(self, trace_path, output_path):
        return generate_workload_xml(trace_path, output_path)
