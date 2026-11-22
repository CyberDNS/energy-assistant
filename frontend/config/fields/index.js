// The reusable field editors of the configuration UI.
//
//   ScalarInput        text / number / yes-no / choice
//   OptionSelect       dropdown over { value, label }
//   EntityPicker       Home Assistant entity id with suggestions
//   SecretPicker       reference to (and creation of) a secret in secrets.yaml
//   ListEditor         ordered list of simple values
//   TableEditor        list of records, one column per field
//   MapEditor          mapping with free keys
//   JsonEditor         raw fallback
//   LoadProfileEditor  static load profile per day type
//   PluginEditor       type + that plugin's settings (tariff, forecast)
//   LearningEditor     on / off / on with options
//   FieldInput         picks the editor for a spec
//   SchemaField        label + FieldInput + hint
//   SchemaForm         grid of SchemaFields for a mapping
//
// They nest in each other (a form holds a table whose cells are fields …), so
// they are registered globally instead of importing one another.
import ScalarInput from './ScalarInput.js'
import OptionSelect from './OptionSelect.js'
import EntityPicker from './EntityPicker.js'
import SecretPicker from './SecretPicker.js'
import ListEditor from './ListEditor.js'
import TableEditor from './TableEditor.js'
import MapEditor from './MapEditor.js'
import JsonEditor from './JsonEditor.js'
import LoadProfileEditor from './LoadProfileEditor.js'
import PluginEditor from './PluginEditor.js'
import LearningEditor from './LearningEditor.js'
import FieldInput from './FieldInput.js'
import SchemaField from './SchemaField.js'
import SchemaForm from './SchemaForm.js'

const COMPONENTS = {
  ScalarInput, OptionSelect, EntityPicker, SecretPicker, ListEditor, TableEditor, MapEditor,
  JsonEditor, LoadProfileEditor, PluginEditor, LearningEditor, FieldInput, SchemaField, SchemaForm,
}

export function registerFieldComponents(app) {
  for (const [name, component] of Object.entries(COMPONENTS)) app.component(name, component)
}
