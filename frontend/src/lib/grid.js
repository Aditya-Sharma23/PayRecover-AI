// AG Grid setup shared by every table: modules registered once, and the dashboard's dark palette.
import { AllCommunityModule, ModuleRegistry, colorSchemeDark, themeQuartz } from "ag-grid-community";

ModuleRegistry.registerModules([AllCommunityModule]);

export const gridTheme = themeQuartz.withPart(colorSchemeDark).withParams({
  backgroundColor: "#11161d",
  foregroundColor: "#e9edf5",
  headerBackgroundColor: "#0c1117",
  borderColor: "#252c35",
  accentColor: "#ed2938",
  fontFamily: "inherit",
  fontSize: 12,
  headerFontWeight: 700,
});
