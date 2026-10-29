"""Packaged malware-report-html template with its original layout and styling."""

from .scripts.render import TEMPLATE_VERSION, render
from .scripts.validate import validate as validate_report

__all__ = ['TEMPLATE_VERSION', 'render_html', 'validate_report']


def render_html(data: dict, *, layout: str = 'auto', toc_on: bool = True) -> str:
    """Validate and render one self-contained report without writing files."""
    if layout not in {'auto', 'horizontal', 'vertical'}:
        raise ValueError(f'Unsupported report layout: {layout!r}')
    errors, _warnings = validate_report(data)
    if errors:
        raise ValueError('Invalid HTML report data:\n' + '\n'.join(errors))
    return render(data, layout=layout, toc_on=toc_on)
